import logging
from collections import OrderedDict
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass
from threading import Lock
from time import monotonic
from unicodedata import normalize

from wsmpld.parser import ParsedSamplesPage, parse_samples_page
from wsmpld.upstream import FetchSamplesPage, SamplesPage, SamplesPageLocation

logger = logging.getLogger("uvicorn.error")


@dataclass(frozen=True)
class CachedSamplesPage:
    source: SamplesPage
    parsed: ParsedSamplesPage


@dataclass(frozen=True)
class _CacheEntry:
    page: CachedSamplesPage
    expires_at: float


class ParsedSamplesPageCache:
    def __init__(
        self,
        *,
        lifetime_seconds: float = 600.0,
        capacity: int = 256,
        monotonic: Callable[[], float] = monotonic,
    ) -> None:
        self._lifetime_seconds = lifetime_seconds
        self._capacity = capacity
        self._monotonic = monotonic
        self._pages: OrderedDict[tuple[str, int], _CacheEntry] = OrderedDict()
        self._inflight: dict[tuple[str, int], Future[CachedSamplesPage]] = {}
        self._lock = Lock()

    def get_or_fetch(
        self,
        artist_slug: str,
        location: SamplesPageLocation | None,
        fetch_samples_page: FetchSamplesPage,
    ) -> CachedSamplesPage:
        page_number = location.page_number if location is not None else 1
        key = (normalize("NFC", artist_slug), page_number)
        with self._lock:
            now = self._monotonic()
            entry = self._pages.get(key)
            if entry is not None and entry.expires_at > now:
                self._pages.move_to_end(key)
                logger.info(
                    "Samples page cache hit artist_slug=%s page=%d",
                    artist_slug,
                    page_number,
                )
                return entry.page
            if entry is not None:
                del self._pages[key]
                logger.info(
                    "Samples page cache expired artist_slug=%s page=%d",
                    artist_slug,
                    page_number,
                )
            pending = self._inflight.get(key)
            leader = pending is None
            if pending is None:
                pending = Future()
                self._inflight[key] = pending

        if not leader:
            logger.info(
                "Samples page cache coalesced artist_slug=%s page=%d",
                artist_slug,
                page_number,
            )
            return pending.result()

        logger.info("Samples page cache miss artist_slug=%s page=%d", artist_slug, page_number)
        try:
            source = (
                fetch_samples_page(artist_slug)
                if location is None
                else fetch_samples_page(artist_slug, location)
            )
            resolved_page_number = source.page_number or page_number
            parsed = parse_samples_page(
                source.html,
                artist_slug=artist_slug,
                page_number=resolved_page_number,
            )
            cached = CachedSamplesPage(source=source, parsed=parsed)
        except BaseException as error:
            with self._lock:
                del self._inflight[key]
            pending.set_exception(error)
            raise

        with self._lock:
            self._pages[key] = _CacheEntry(
                page=cached,
                expires_at=self._monotonic() + self._lifetime_seconds,
            )
            if len(self._pages) > self._capacity:
                self._pages.popitem(last=False)
            del self._inflight[key]
        pending.set_result(cached)
        return cached
