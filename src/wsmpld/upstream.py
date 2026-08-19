import logging
from collections.abc import Callable
from dataclasses import dataclass
from threading import Lock
from time import monotonic, time
from typing import Any, Protocol
from urllib.parse import quote

BASE_URL = "https://www.whosampled.com"
CLEARANCE_TIMEOUT_SECONDS = 90.0
FETCH_TIMEOUT_SECONDS = 20.0
LOOKUP_TIMEOUT_SECONDS = 120.0

logger = logging.getLogger("uvicorn.error")


@dataclass(frozen=True)
class SamplesPage:
    html: str
    resolved_url: str


@dataclass(frozen=True)
class ClearanceSession:
    cookies: dict[str, str]
    user_agent: str
    expires_at: float


@dataclass(frozen=True)
class BrowserlessResponse:
    status_code: int
    text: str
    resolved_url: str


class FetchSamplesPage(Protocol):
    def __call__(self, artist_slug: str) -> SamplesPage: ...


class ArtistNotFoundError(Exception):
    """The upstream definitively reported that the requested artist does not exist."""


class ClearanceFailedError(Exception):
    """A reusable upstream clearance session could not be acquired."""


class LookupTimeoutError(Exception):
    """The complete upstream lookup exceeded its operation deadline."""


class AcquireClearance(Protocol):
    def __call__(self, timeout: float) -> ClearanceSession: ...


class FetchBrowserlessly(Protocol):
    def __call__(
        self, url: str, clearance: ClearanceSession, timeout: float
    ) -> BrowserlessResponse: ...


class CamoufoxClearanceAcquirer:
    def __call__(self, timeout: float) -> ClearanceSession:
        from camoufox.sync_api import Camoufox
        from playwright.sync_api import Error as PlaywrightError

        logger.info("visible unattended Camoufox clearance acquisition started")
        deadline = monotonic() + timeout
        with Camoufox(  # type: ignore[no-untyped-call]
            headless=False,
            humanize=True,
            os="macos",
            locale="en-US",
            disable_coop=True,
            i_know_what_im_doing=True,
        ) as browser:
            page = browser.new_page()
            page.goto(
                f"{BASE_URL}/",
                timeout=max(1, int((deadline - monotonic()) * 1_000)),
                wait_until="domcontentloaded",
            )
            interaction_attempts = 0
            next_interaction = 0.0
            while monotonic() < deadline:
                cookies = page.context.cookies()
                clearance_cookie = next(
                    (cookie for cookie in cookies if cookie.get("name") == "cf_clearance"),
                    None,
                )
                if clearance_cookie is not None:
                    user_agent = str(page.evaluate("navigator.userAgent"))
                    expires = float(clearance_cookie.get("expires", -1))
                    lifetime = expires - time()
                    if lifetime <= 0:
                        lifetime = 30 * 60
                    return ClearanceSession(
                        cookies={str(cookie["name"]): str(cookie["value"]) for cookie in cookies},
                        user_agent=user_agent,
                        expires_at=monotonic() + lifetime,
                    )
                now = monotonic()
                if now >= next_interaction and interaction_attempts < 3:
                    for frame in page.frames:
                        if "challenges.cloudflare.com" not in frame.url:
                            continue
                        try:
                            body = frame.locator("body")
                            if body.bounding_box() is None:
                                continue
                            interaction_attempts += 1
                            next_interaction = now + 12
                            logger.info(
                                "clearance challenge interaction attempt=%d",
                                interaction_attempts,
                            )
                            body.click(position={"x": 28, "y": 32}, timeout=5_000)
                            break
                        except PlaywrightError:
                            continue
                remaining_milliseconds = max(1, int((deadline - monotonic()) * 1_000))
                page.wait_for_timeout(min(500, remaining_milliseconds))
        raise RuntimeError("Camoufox did not acquire cf_clearance within its budget")


class CurlCffiBrowserlessFetcher:
    def __init__(self) -> None:
        self._session: Any | None = None

    def __call__(
        self, url: str, clearance: ClearanceSession, timeout: float
    ) -> BrowserlessResponse:
        logger.info("Samples data fetch started transport=curl_cffi")
        if self._session is None:
            from curl_cffi import requests

            self._session = requests.Session(impersonate="firefox135")
        response = self._session.get(
            url,
            cookies=clearance.cookies,
            headers={
                "User-Agent": clearance.user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            },
            timeout=timeout,
            allow_redirects=True,
        )
        return BrowserlessResponse(
            status_code=int(response.status_code),
            text=str(response.text),
            resolved_url=str(response.url),
        )


class BrowserlessSamplesPage:
    def __init__(
        self,
        *,
        acquire_clearance: AcquireClearance,
        fetch_browserlessly: FetchBrowserlessly,
        monotonic: Callable[[], float] = monotonic,
    ) -> None:
        self._acquire_clearance = acquire_clearance
        self._fetch_browserlessly = fetch_browserlessly
        self._monotonic = monotonic
        self._clearance: ClearanceSession | None = None
        self._lock = Lock()

    def __call__(self, artist_slug: str) -> SamplesPage:
        deadline = self._monotonic() + LOOKUP_TIMEOUT_SECONDS
        acquired_lock = self._lock.acquire(timeout=self._remaining(deadline))
        if not acquired_lock:
            raise LookupTimeoutError("Timed out waiting for upstream session")
        try:
            clearance = self._clearance
            if clearance is None:
                clearance = self._acquire(deadline)
                self._clearance = clearance
            elif clearance.expires_at <= self._monotonic():
                logger.info("discarding expired clearance session")
                self._clearance = None
                clearance = self._acquire(deadline)
                self._clearance = clearance
            else:
                logger.info("reusing unexpired clearance session")
            url = f"{BASE_URL}/{quote(artist_slug, safe='')}/samples/"
            response = self._fetch(url, clearance, deadline)
            if _is_challenge(response):
                logger.info("browserless Samples fetch challenged; refreshing clearance")
                self._clearance = None
                clearance = self._acquire(deadline)
                self._clearance = clearance
                response = self._fetch(url, clearance, deadline)
                if _is_challenge(response):
                    self._clearance = None
                    raise ClearanceFailedError("Browserless retry was challenged")
            if response.status_code == 404:
                raise ArtistNotFoundError(artist_slug)
            if response.status_code != 200:
                raise RuntimeError(f"Unexpected upstream status {response.status_code}")
            return SamplesPage(html=response.text, resolved_url=response.resolved_url)
        finally:
            self._lock.release()

    def _remaining(self, deadline: float, maximum: float = LOOKUP_TIMEOUT_SECONDS) -> float:
        remaining = deadline - self._monotonic()
        if remaining <= 0:
            raise LookupTimeoutError("Complete lookup deadline exceeded")
        return min(maximum, remaining)

    def _raise_if_deadline_expired(self, deadline: float, error: Exception) -> None:
        try:
            self._remaining(deadline)
        except LookupTimeoutError as timeout_error:
            raise timeout_error from error

    def _acquire(self, deadline: float) -> ClearanceSession:
        logger.info("clearance acquisition started")
        acquisition_timeout = self._remaining(deadline, CLEARANCE_TIMEOUT_SECONDS)
        try:
            clearance = self._acquire_clearance(acquisition_timeout)
        except Exception as error:
            self._raise_if_deadline_expired(deadline, error)
            raise ClearanceFailedError("Clearance acquisition failed") from error
        self._remaining(deadline)
        logger.info("clearance acquisition completed")
        return clearance

    def _fetch(
        self, url: str, clearance: ClearanceSession, deadline: float
    ) -> BrowserlessResponse:
        logger.info("browserless Samples fetch started url=%s", url)
        try:
            response = self._fetch_browserlessly(
                url,
                clearance,
                self._remaining(deadline, FETCH_TIMEOUT_SECONDS),
            )
        except Exception as error:
            self._raise_if_deadline_expired(deadline, error)
            raise
        self._remaining(deadline)
        logger.info("browserless Samples fetch completed status=%d", response.status_code)
        return response


def _is_challenge(response: BrowserlessResponse) -> bool:
    normalized = response.text.lower()
    return "<title>just a moment" in normalized or "cf-challenge" in normalized


live_samples_page = BrowserlessSamplesPage(
    acquire_clearance=CamoufoxClearanceAcquirer(),
    fetch_browserlessly=CurlCffiBrowserlessFetcher(),
)
