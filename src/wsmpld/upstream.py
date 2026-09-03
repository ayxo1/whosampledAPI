import logging
import random
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from threading import Lock
from time import monotonic, sleep, time
from typing import Any, Protocol
from urllib.parse import quote, urlsplit

from wsmpld.samples_url import BASE_URL, resolved_samples_page_number

CLEARANCE_TIMEOUT_SECONDS = 90.0
FETCH_TIMEOUT_SECONDS = 20.0
LOOKUP_TIMEOUT_SECONDS = 120.0
MINIMUM_REQUEST_INTERVAL_SECONDS = 4.0
MINIMUM_REQUEST_JITTER_SECONDS = 0.0
MAXIMUM_REQUEST_JITTER_SECONDS = 1.0
CURL_CFFI_FIREFOX_MAJOR_VERSION = 135

_FIREFOX_VERSION_PATTERN = re.compile(r"Firefox/(?P<major>[0-9]+)(?:\.|$)")

_HTTP_DATE_PATTERN = re.compile(
    r"(?:"
    r"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun), [0-9]{2} "
    r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) "
    r"[0-9]{4} [0-9]{2}:[0-9]{2}:[0-9]{2} GMT"
    r"|(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday), "
    r"[0-9]{2}-(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)-[0-9]{2} "
    r"[0-9]{2}:[0-9]{2}:[0-9]{2} GMT"
    r"|(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun) "
    r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) "
    r"(?: [0-9]|[0-9]{2}) [0-9]{2}:[0-9]{2}:[0-9]{2} [0-9]{4}"
    r")"
)

logger = logging.getLogger("uvicorn.error")


def _require_aware_timestamp(value: datetime, *, label: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must include a UTC offset")


def firefox_major_version(user_agent: str) -> int:
    match = _FIREFOX_VERSION_PATTERN.search(user_agent)
    if match is None:
        raise ValueError("Clearance User-Agent has no Firefox version")
    return int(match.group("major"))


@dataclass(frozen=True)
class SamplesPage:
    html: str
    resolved_url: str
    page_number: int | None = None
    fetched_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if self.page_number is not None and (
            type(self.page_number) is not int or self.page_number < 1
        ):
            raise ValueError("Resolved Samples page must be a positive integer")
        _require_aware_timestamp(self.fetched_at, label="Samples fetch time")


@dataclass(frozen=True)
class SampleUsePage:
    html: str
    resolved_url: str


@dataclass(frozen=True)
class SamplesPageLocation:
    page_number: int

    def __post_init__(self) -> None:
        if type(self.page_number) is not int or self.page_number < 2:
            raise ValueError("Samples continuation page must be an integer greater than one")


@dataclass(frozen=True)
class ClearanceSession:
    cookies: dict[str, str]
    user_agent: str
    expires_at: float
    firefox_major_version: int = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "firefox_major_version",
            firefox_major_version(self.user_agent),
        )


@dataclass(frozen=True)
class BrowserlessResponse:
    status_code: int
    text: str
    resolved_url: str
    headers: Mapping[str, str] = field(default_factory=dict)
    fetched_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        _require_aware_timestamp(
            self.fetched_at,
            label="Browserless response fetch time",
        )


class FetchSamplesPage(Protocol):
    def __call__(
        self,
        artist_slug: str,
        location: SamplesPageLocation | None = None,
    ) -> SamplesPage: ...


class FetchSampleUsePage(Protocol):
    def __call__(self, sample_use_id: int) -> SampleUsePage: ...


class ArtistNotFoundError(Exception):
    """The upstream definitively reported that the requested artist does not exist."""


class SampleUseNotFoundError(Exception):
    """The upstream definitively reported that the requested Sample Use does not exist."""


class ClearanceFailedError(Exception):
    """A reusable upstream clearance session could not be acquired."""


class LookupTimeoutError(Exception):
    """The complete upstream lookup exceeded its operation deadline."""


class UpstreamRateLimitedError(Exception):
    """WhoSampled rejected the data request because its rate limit was reached."""

    def __init__(self, retry_after: str | None) -> None:
        super().__init__("WhoSampled rate limited the request")
        self.retry_after = retry_after


class AcquireClearance(Protocol):
    def __call__(self, timeout: float) -> ClearanceSession: ...


class FetchBrowserlessly(Protocol):
    def __call__(
        self, url: str, clearance: ClearanceSession, timeout: float
    ) -> BrowserlessResponse: ...


class ValidateBrowserlessResponse(Protocol):
    def __call__(self, response: BrowserlessResponse) -> None: ...


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
                try:
                    page_is_usable = _is_usable_who_sampled_page(
                        str(page.url),
                        str(page.content()),
                    )
                except PlaywrightError:
                    page_is_usable = False
                if clearance_cookie is not None and page_is_usable:
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
                if now >= next_interaction:
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

            self._session = requests.Session(
                impersonate=f"firefox{clearance.firefox_major_version}"
            )
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
            headers=dict(response.headers),
        )


class BrowserlessWhoSampledSession:
    def __init__(
        self,
        *,
        acquire_clearance: AcquireClearance,
        fetch_browserlessly: FetchBrowserlessly,
        monotonic: Callable[[], float] = monotonic,
        sleep: Callable[[float], None] = sleep,
        jitter: Callable[[float, float], float] = random.uniform,
        minimum_interval_seconds: float = MINIMUM_REQUEST_INTERVAL_SECONDS,
        minimum_jitter_seconds: float = MINIMUM_REQUEST_JITTER_SECONDS,
        maximum_jitter_seconds: float = MAXIMUM_REQUEST_JITTER_SECONDS,
    ) -> None:
        self._acquire_clearance = acquire_clearance
        self._fetch_browserlessly = fetch_browserlessly
        self._monotonic = monotonic
        self._sleep = sleep
        self._jitter = jitter
        self._minimum_interval_seconds = minimum_interval_seconds
        self._minimum_jitter_seconds = minimum_jitter_seconds
        self._maximum_jitter_seconds = maximum_jitter_seconds
        self._clearance: ClearanceSession | None = None
        self._last_request_started_at: float | None = None
        self._lock = Lock()

    def fetch(
        self,
        url: str,
        *,
        validate_response: ValidateBrowserlessResponse,
        resource_name: str,
        log_url: bool = False,
    ) -> BrowserlessResponse:
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

            def fetch_validated(current_clearance: ClearanceSession) -> BrowserlessResponse:
                fetched = self._fetch(
                    url,
                    current_clearance,
                    deadline,
                    resource_name=resource_name,
                    log_url=log_url,
                )
                _raise_if_rate_limited(fetched)
                validate_response(fetched)
                return fetched

            response = fetch_validated(clearance)
            if _is_challenge(response):
                logger.info(
                    "browserless %s fetch challenged; refreshing clearance",
                    resource_name,
                )
                self._clearance = None
                clearance = self._acquire(deadline)
                self._clearance = clearance
                response = fetch_validated(clearance)
                if _is_challenge(response):
                    self._clearance = None
                    raise ClearanceFailedError("Browserless retry was challenged")
            return response
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
        if clearance.firefox_major_version != CURL_CFFI_FIREFOX_MAJOR_VERSION:
            logger.warning(
                "clearance fingerprint incompatible firefox_major=%d expected=%d",
                clearance.firefox_major_version,
                CURL_CFFI_FIREFOX_MAJOR_VERSION,
            )
            raise ClearanceFailedError("Clearance browser fingerprint is incompatible")
        self._remaining(deadline)
        logger.info(
            "clearance fingerprint compatible firefox_major=%d",
            clearance.firefox_major_version,
        )
        logger.info("clearance acquisition completed")
        return clearance

    def _fetch(
        self,
        url: str,
        clearance: ClearanceSession,
        deadline: float,
        *,
        resource_name: str,
        log_url: bool,
    ) -> BrowserlessResponse:
        self._pace(deadline, resource_name)
        request_started_at = self._monotonic()
        if log_url:
            logger.info(
                "browserless %s fetch started url=%s started_at=%.3f",
                resource_name,
                url,
                request_started_at,
            )
        else:
            logger.info(
                "browserless %s fetch started started_at=%.3f",
                resource_name,
                request_started_at,
            )
        self._last_request_started_at = request_started_at
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
        logger.info(
            "browserless %s fetch completed status=%d",
            resource_name,
            response.status_code,
        )
        return response

    def _pace(self, deadline: float, resource_name: str) -> None:
        if self._last_request_started_at is None:
            return
        jitter = self._jitter(
            self._minimum_jitter_seconds,
            self._maximum_jitter_seconds,
        )
        next_request_at = (
            self._last_request_started_at + self._minimum_interval_seconds + jitter
        )
        delay = next_request_at - self._monotonic()
        if delay <= 0:
            return
        logger.info("%s request pacing wait_seconds=%.3f", resource_name, delay)
        remaining = self._remaining(deadline)
        self._sleep(min(delay, remaining))
        self._remaining(deadline)


class BrowserlessSamplesPage:
    def __init__(
        self,
        *,
        session: BrowserlessWhoSampledSession,
    ) -> None:
        self._session = session

    def __call__(
        self,
        artist_slug: str,
        location: SamplesPageLocation | None = None,
    ) -> SamplesPage:
        if location is not None and not isinstance(location, SamplesPageLocation):
            raise TypeError("location must be an internal Samples page location")
        url = f"{BASE_URL}/{quote(artist_slug, safe='')}/samples/"
        if location is not None:
            url = f"{url}?sp={location.page_number}"
        requested_page = location.page_number if location is not None else 1
        page_number = requested_page

        def validate_response(response: BrowserlessResponse) -> None:
            nonlocal page_number
            page_number = _validate_resolved_samples_url(
                response.resolved_url,
                artist_slug=artist_slug,
                requested_page=requested_page,
            )

        response = self._session.fetch(
            url,
            validate_response=validate_response,
            resource_name="Samples",
            log_url=True,
        )
        if response.status_code == 404:
            raise ArtistNotFoundError(artist_slug)
        if response.status_code != 200:
            raise RuntimeError(f"Unexpected upstream status {response.status_code}")
        return SamplesPage(
            html=response.text,
            resolved_url=response.resolved_url,
            page_number=page_number,
            fetched_at=response.fetched_at,
        )


class BrowserlessSampleUsePage:
    def __init__(self, *, session: BrowserlessWhoSampledSession) -> None:
        self._session = session

    def __call__(self, sample_use_id: int) -> SampleUsePage:
        if type(sample_use_id) is not int or sample_use_id < 1:
            raise TypeError("sample_use_id must be a positive integer")
        url = f"{BASE_URL}/sample/{sample_use_id}/"

        def validate_response(response: BrowserlessResponse) -> None:
            _validate_resolved_sample_use_url(
                response.resolved_url,
                sample_use_id=sample_use_id,
            )

        response = self._session.fetch(
            url,
            validate_response=validate_response,
            resource_name="Sample Use",
        )
        if response.status_code == 404:
            raise SampleUseNotFoundError(sample_use_id)
        if response.status_code != 200:
            raise RuntimeError(f"Unexpected upstream status {response.status_code}")
        return SampleUsePage(
            html=response.text,
            resolved_url=response.resolved_url,
        )


def _is_challenge(response: BrowserlessResponse) -> bool:
    return _is_challenge_html(response.text)


def _is_challenge_html(html: str) -> bool:
    normalized = html.lower()
    return "<title>just a moment" in normalized or "cf-challenge" in normalized


def _is_usable_who_sampled_page(resolved_url: str, html: str) -> bool:
    parsed = urlsplit(resolved_url)
    return (
        parsed.scheme == "https"
        and parsed.hostname == "www.whosampled.com"
        and not _is_challenge_html(html)
    )


def _raise_if_rate_limited(response: BrowserlessResponse) -> None:
    if response.status_code != 429:
        return
    retry_after = next(
        (
            value
            for name, value in response.headers.items()
            if name.lower() == "retry-after"
        ),
        None,
    )
    raise UpstreamRateLimitedError(_valid_retry_after(retry_after))


def _valid_retry_after(value: str | None) -> str | None:
    if value is None:
        return None
    if re.fullmatch(r"[0-9]+", value):
        return value
    if _HTTP_DATE_PATTERN.fullmatch(value) is None:
        return None
    try:
        parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return value


def _validate_resolved_samples_url(
    resolved_url: str,
    *,
    artist_slug: str,
    requested_page: int,
) -> int:
    try:
        resolved_page = resolved_samples_page_number(resolved_url, artist_slug)
    except ValueError as error:
        raise RuntimeError("Invalid Samples redirect destination") from error

    if resolved_page < requested_page:
        raise RuntimeError("Samples redirect moved backward")
    return resolved_page


def _validate_resolved_sample_use_url(
    resolved_url: str,
    *,
    sample_use_id: int,
) -> None:
    parsed = urlsplit(resolved_url)
    match = re.fullmatch(r"/sample/([1-9][0-9]*)/(?:[^/]+/)?", parsed.path)
    if (
        parsed.scheme != "https"
        or parsed.netloc != "www.whosampled.com"
        or parsed.query
        or parsed.fragment
        or match is None
        or int(match.group(1)) != sample_use_id
    ):
        raise RuntimeError("Invalid Sample Use redirect destination")


live_who_sampled_session = BrowserlessWhoSampledSession(
    acquire_clearance=CamoufoxClearanceAcquirer(),
    fetch_browserlessly=CurlCffiBrowserlessFetcher(),
)
live_samples_page = BrowserlessSamplesPage(session=live_who_sampled_session)
live_sample_use_page = BrowserlessSampleUsePage(session=live_who_sampled_session)
