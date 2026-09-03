import base64
import json
from collections.abc import Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from contextlib import contextmanager
from pathlib import Path
from threading import Event, Lock
from urllib.parse import quote

import pytest
from curl_cffi.requests.exceptions import TooManyRedirects
from fastapi.testclient import TestClient

from wsmpld.api import app, get_samples_page, get_samples_page_cache
from wsmpld.page_cache import ParsedSamplesPageCache
from wsmpld.upstream import (
    ArtistNotFoundError,
    BrowserlessResponse,
    BrowserlessSamplesPage,
    BrowserlessWhoSampledSession,
    ClearanceFailedError,
    ClearanceSession,
    FetchBrowserlessly,
    FetchSamplesPage,
    LookupTimeoutError,
    SamplesPage,
    SamplesPageLocation,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _opaque_cursor_payload(payload: object) -> str:
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode()
    )
    return encoded.rstrip(b"=").decode("ascii")


@contextmanager
def _override_samples_page(
    fetch_samples_page: FetchSamplesPage,
    *,
    page_cache: ParsedSamplesPageCache | None = None,
) -> Iterator[None]:
    page_cache = page_cache or ParsedSamplesPageCache()
    app.dependency_overrides[get_samples_page] = lambda: fetch_samples_page
    app.dependency_overrides[get_samples_page_cache] = lambda: page_cache
    try:
        yield
    finally:
        app.dependency_overrides.clear()


def _browserless_page(fetch_browserlessly: FetchBrowserlessly) -> BrowserlessSamplesPage:
    return BrowserlessSamplesPage(
        acquire_clearance=lambda timeout: ClearanceSession(
            cookies={"cf_clearance": "secret"},
            user_agent="test-agent",
            expires_at=10_000.0,
        ),
        fetch_browserlessly=fetch_browserlessly,
        monotonic=lambda: 0.0,
        sleep=lambda delay: None,
        minimum_interval_seconds=0.0,
        minimum_jitter_seconds=0.0,
        maximum_jitter_seconds=0.0,
    )


def test_user_receives_one_sample_use_by_default() -> None:
    page = SamplesPage(
        html=(FIXTURES / "one_sample_use.html").read_text(encoding="utf-8"),
        resolved_url="https://www.whosampled.com/Kanye-West/samples/",
    )
    with _override_samples_page(lambda artist_slug: page):
        response = TestClient(app).get("/artists/Kanye-West/samples")

    assert response.status_code == 200
    assert response.json() == {
        "artist": {
            "requested_slug": "Kanye-West",
            "name": "Kanye West",
            "samples_url": "https://www.whosampled.com/Kanye-West/samples/",
        },
        "items": [
            {
                "sampling_recording": {
                    "title": "Power",
                    "artist_credit": "Kanye West",
                    "year": 2010,
                    "producer_credit": "Kanye West, Symbolyc One",
                    "url": "https://www.whosampled.com/Kanye-West/Power/",
                },
                "source_recording": {
                    "title": "21st Century Schizoid Man",
                    "artist_credit": "King Crimson",
                    "year": 1969,
                    "url": "https://www.whosampled.com/King-Crimson/21st-Century-Schizoid-Man/",
                },
            }
        ],
        "pagination": {"next_cursor": None, "returned": 1, "has_more": False},
    }


def test_uncached_requests_use_default_process_wide_spacing_and_jitter(
    caplog: pytest.LogCaptureFixture,
) -> None:
    page_html = (FIXTURES / "one_sample_use.html").read_text(encoding="utf-8")
    current_time = 0.0
    fetch_times: list[float] = []
    sleeps: list[float] = []
    jitter_bounds: list[tuple[float, float]] = []

    def monotonic() -> float:
        return current_time

    def sleep(delay: float) -> None:
        nonlocal current_time
        sleeps.append(delay)
        current_time += delay

    def jitter(lower: float, upper: float) -> float:
        jitter_bounds.append((lower, upper))
        return 0.25

    def fetch_browserlessly(
        url: str, clearance: ClearanceSession, timeout: float
    ) -> BrowserlessResponse:
        fetch_times.append(current_time)
        return BrowserlessResponse(status_code=200, text=page_html, resolved_url=url)

    fetch_samples_page = BrowserlessSamplesPage(
        acquire_clearance=lambda timeout: ClearanceSession(
            cookies={"cf_clearance": "secret"},
            user_agent="test-agent",
            expires_at=10_000.0,
        ),
        fetch_browserlessly=fetch_browserlessly,
        monotonic=monotonic,
        sleep=sleep,
        jitter=jitter,
    )

    client = TestClient(app)
    with caplog.at_level("INFO"), _override_samples_page(fetch_samples_page):
        first_response = client.get("/artists/Kanye-West/samples")
        second_response = client.get("/artists/Jay-Z/samples")

    assert [first_response.status_code, second_response.status_code] == [200, 200]
    assert fetch_times == [0.0, 4.25]
    assert sleeps == [4.25]
    assert jitter_bounds == [(0.0, 1.0)]
    assert "Samples request pacing wait_seconds=4.250" in [
        record.getMessage() for record in caplog.records
    ]
    assert [
        message
        for message in (record.getMessage() for record in caplog.records)
        if "browserless Samples fetch started" in message
    ] == [
        "browserless Samples fetch started "
        "url=https://www.whosampled.com/Kanye-West/samples/ started_at=0.000",
        "browserless Samples fetch started "
        "url=https://www.whosampled.com/Jay-Z/samples/ started_at=4.250",
    ]


def test_artist_and_future_resource_share_clearance_session_and_pacing() -> None:
    page_html = (FIXTURES / "one_sample_use.html").read_text(encoding="utf-8")
    current_time = 0.0
    acquisitions = 0
    requested_urls: list[str] = []
    sleeps: list[float] = []

    def acquire_clearance(timeout: float) -> ClearanceSession:
        nonlocal acquisitions
        acquisitions += 1
        return ClearanceSession(
            cookies={"cf_clearance": "secret"},
            user_agent="test-agent",
            expires_at=10_000.0,
        )

    def fetch_browserlessly(
        url: str, clearance: ClearanceSession, timeout: float
    ) -> BrowserlessResponse:
        requested_urls.append(url)
        return BrowserlessResponse(status_code=200, text=page_html, resolved_url=url)

    def sleep(delay: float) -> None:
        nonlocal current_time
        sleeps.append(delay)
        current_time += delay

    session = BrowserlessWhoSampledSession(
        acquire_clearance=acquire_clearance,
        fetch_browserlessly=fetch_browserlessly,
        monotonic=lambda: current_time,
        sleep=sleep,
        jitter=lambda lower, upper: 0.0,
    )
    fetch_samples_page = BrowserlessSamplesPage(session=session)

    with _override_samples_page(fetch_samples_page):
        artist_response = TestClient(app).get("/artists/Kanye-West/samples")
        resource_response = session.fetch(
            "https://www.whosampled.com/sample/123/",
            validate_response=lambda response: None,
            resource_name="Sample Use",
        )

    assert artist_response.status_code == 200
    assert resource_response.status_code == 200
    assert acquisitions == 1
    assert requested_urls == [
        "https://www.whosampled.com/Kanye-West/samples/",
        "https://www.whosampled.com/sample/123/",
    ]
    assert sleeps == [4.0]


def test_cache_hit_returns_without_pacing_or_an_upstream_request(
    caplog: pytest.LogCaptureFixture,
) -> None:
    page_html = (FIXTURES / "multiple_sample_uses.html").read_text(encoding="utf-8")
    fetches = 0
    sleeps = 0
    jitter_calls = 0

    def fetch_browserlessly(
        url: str, clearance: ClearanceSession, timeout: float
    ) -> BrowserlessResponse:
        nonlocal fetches
        fetches += 1
        return BrowserlessResponse(status_code=200, text=page_html, resolved_url=url)

    def sleep(delay: float) -> None:
        nonlocal sleeps
        sleeps += 1

    def jitter(lower: float, upper: float) -> float:
        nonlocal jitter_calls
        jitter_calls += 1
        return 0.0

    fetch_samples_page = BrowserlessSamplesPage(
        acquire_clearance=lambda timeout: ClearanceSession(
            cookies={"cf_clearance": "secret"},
            user_agent="test-agent",
            expires_at=10_000.0,
        ),
        fetch_browserlessly=fetch_browserlessly,
        monotonic=lambda: 0.0,
        sleep=sleep,
        jitter=jitter,
    )

    client = TestClient(app)
    with caplog.at_level("INFO"), _override_samples_page(fetch_samples_page):
        first_response = client.get("/artists/Kanye-West/samples?limit=1")
        cursor = first_response.json()["pagination"]["next_cursor"]
        cached_response = client.get(
            "/artists/Kanye-West/samples",
            params={"cursor": cursor, "limit": "max"},
        )

    assert [first_response.status_code, cached_response.status_code] == [200, 200]
    assert fetches == 1
    assert sleeps == 0
    assert jitter_calls == 0
    messages = [record.getMessage() for record in caplog.records]
    assert any("Samples page cache miss" in message for message in messages)
    assert any("Samples page cache hit" in message for message in messages)
    assert not any("Samples request pacing" in message for message in messages)


def test_upstream_rate_limit_returns_stable_service_unavailable_without_retry(
    caplog: pytest.LogCaptureFixture,
) -> None:
    acquisitions = 0
    fetches = 0

    def acquire_clearance(timeout: float) -> ClearanceSession:
        nonlocal acquisitions
        acquisitions += 1
        return ClearanceSession(
            cookies={"cf_clearance": "secret"},
            user_agent="test-agent",
            expires_at=10_000.0,
        )

    def fetch_browserlessly(
        url: str, clearance: ClearanceSession, timeout: float
    ) -> BrowserlessResponse:
        nonlocal fetches
        fetches += 1
        return BrowserlessResponse(
            status_code=429,
            text="do-not-log-upstream-body",
            resolved_url=url,
            headers={"Retry-After": "120", "X-Secret": "do-not-log-header"},
        )

    fetch_samples_page = BrowserlessSamplesPage(
        acquire_clearance=acquire_clearance,
        fetch_browserlessly=fetch_browserlessly,
        monotonic=lambda: 0.0,
    )

    with caplog.at_level("INFO"), _override_samples_page(fetch_samples_page):
        response = TestClient(app).get("/artists/Kanye-West/samples")

    assert response.status_code == 503
    assert response.json() == {
        "detail": {
            "code": "upstream_rate_limited",
            "message": "WhoSampled rate limited the request.",
        }
    }
    assert response.headers["retry-after"] == "120"
    assert acquisitions == 1
    assert fetches == 1
    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "code=upstream_rate_limited" in messages
    assert "retry_after_forwarded=True" in messages
    assert "do-not-log-upstream-body" not in messages
    assert "do-not-log-header" not in messages
    assert "120" not in messages


def test_rate_limit_redirect_returns_stable_service_unavailable_without_retry() -> None:
    fetches = 0

    def fetch_browserlessly(
        url: str, clearance: ClearanceSession, timeout: float
    ) -> BrowserlessResponse:
        nonlocal fetches
        fetches += 1
        return BrowserlessResponse(
            status_code=429,
            text="rate limited",
            resolved_url="https://www.whosampled.com/rate-limit/",
        )

    fetch_samples_page = _browserless_page(fetch_browserlessly)

    with _override_samples_page(fetch_samples_page):
        response = TestClient(app).get("/artists/Kanye-West/samples")

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "upstream_rate_limited"
    assert fetches == 1


@pytest.mark.parametrize(
    ("retry_after", "expected"),
    [
        ("Wed, 21 Oct 2015 07:28:00 GMT", "Wed, 21 Oct 2015 07:28:00 GMT"),
        ("Sunday, 06-Nov-94 08:49:37 GMT", "Sunday, 06-Nov-94 08:49:37 GMT"),
        ("Sun Nov  6 08:49:37 1994", "Sun Nov  6 08:49:37 1994"),
        ("not a delay or date", None),
        ("120 seconds", None),
        ("Wed, 21 Oct 2015 07:28:00 +0000", None),
        ("Wed, 21 Oct 2015 07:28:00 UTC", None),
        ("21 Oct 2015 07:28:00 GMT", None),
        ("Wed, 21 Oct 2015 07:28 GMT", None),
        ("", None),
    ],
)
def test_rate_limit_forwards_only_valid_retry_after_values(
    retry_after: str,
    expected: str | None,
) -> None:
    fetch_samples_page = _browserless_page(
        lambda url, clearance, timeout: BrowserlessResponse(
            status_code=429,
            text="rate limited",
            resolved_url=url,
            headers={"rEtRy-AfTeR": retry_after},
        )
    )

    with _override_samples_page(fetch_samples_page):
        response = TestClient(app).get("/artists/Kanye-West/samples")

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "upstream_rate_limited"
    assert response.headers.get("retry-after") == expected


def test_repeated_cursors_within_one_source_page_reuse_the_parsed_page() -> None:
    page = SamplesPage(
        html=(FIXTURES / "multiple_sample_uses.html").read_text(encoding="utf-8"),
        resolved_url="https://www.whosampled.com/Kanye-West/samples/",
    )
    requested_locations: list[SamplesPageLocation | None] = []

    def fetch(
        artist_slug: str,
        location: SamplesPageLocation | None = None,
    ) -> SamplesPage:
        requested_locations.append(location)
        return page

    client = TestClient(app)
    with _override_samples_page(fetch):
        first_response = client.get("/artists/Kanye-West/samples?limit=1")
        cursor = first_response.json()["pagination"]["next_cursor"]
        second_response = client.get(
            "/artists/Kanye-West/samples",
            params={"cursor": cursor, "limit": 20},
        )

    assert first_response.status_code == 200
    assert [
        item["sampling_recording"]["title"] for item in first_response.json()["items"]
    ] == ["Famous"]
    assert first_response.json()["pagination"] == {
        "next_cursor": cursor,
        "returned": 1,
        "has_more": True,
    }
    assert isinstance(cursor, str) and cursor
    assert second_response.status_code == 200
    assert [
        item["sampling_recording"]["title"] for item in second_response.json()["items"]
    ] == ["Power"]
    assert second_response.json()["pagination"] == {
        "next_cursor": None,
        "returned": 1,
        "has_more": False,
    }
    assert requested_locations == [None]


def test_valid_cursor_continues_after_parsed_page_expires() -> None:
    current_time = 0.0
    page = SamplesPage(
        html=(FIXTURES / "multiple_sample_uses.html").read_text(encoding="utf-8"),
        resolved_url="https://www.whosampled.com/Kanye-West/samples/",
    )
    fetches = 0

    def fetch(artist_slug: str) -> SamplesPage:
        nonlocal fetches
        fetches += 1
        return page

    page_cache = ParsedSamplesPageCache(monotonic=lambda: current_time)
    client = TestClient(app)
    with _override_samples_page(fetch, page_cache=page_cache):
        first_response = client.get("/artists/Kanye-West/samples?limit=1")
        cursor = first_response.json()["pagination"]["next_cursor"]
        cached_response = client.get(
            "/artists/Kanye-West/samples",
            params={"cursor": cursor, "limit": "max"},
        )
        current_time = 600.0
        expired_response = client.get(
            "/artists/Kanye-West/samples",
            params={"cursor": cursor, "limit": "max"},
        )

    assert [
        first_response.status_code,
        cached_response.status_code,
        expired_response.status_code,
    ] == [200, 200, 200]
    assert [
        item["sampling_recording"]["title"]
        for item in expired_response.json()["items"]
    ] == ["Power"]
    assert fetches == 2


def test_valid_cursor_continues_after_parsed_page_is_evicted() -> None:
    page_html = (FIXTURES / "multiple_sample_uses.html").read_text(encoding="utf-8")
    fetches: dict[str, int] = {}

    def fetch(artist_slug: str) -> SamplesPage:
        fetches[artist_slug] = fetches.get(artist_slug, 0) + 1
        return SamplesPage(
            html=page_html,
            resolved_url=f"https://www.whosampled.com/{artist_slug}/samples/",
        )

    client = TestClient(app)
    with _override_samples_page(fetch, page_cache=ParsedSamplesPageCache(capacity=1)):
        first_response = client.get("/artists/Kanye-West/samples?limit=1")
        cursor = first_response.json()["pagination"]["next_cursor"]
        eviction_response = client.get("/artists/Jay-Z/samples")
        continued_response = client.get(
            "/artists/Kanye-West/samples",
            params={"cursor": cursor, "limit": "max"},
        )

    assert [
        first_response.status_code,
        eviction_response.status_code,
        continued_response.status_code,
    ] == [200, 200, 200]
    assert [
        item["sampling_recording"]["title"]
        for item in continued_response.json()["items"]
    ] == ["Power"]
    assert fetches == {"Kanye-West": 2, "Jay-Z": 1}


def test_least_recently_used_page_is_fetched_again_after_capacity_eviction() -> None:
    page_html = (FIXTURES / "one_sample_use.html").read_text(encoding="utf-8")
    fetches: dict[str, int] = {}

    def fetch(artist_slug: str) -> SamplesPage:
        fetches[artist_slug] = fetches.get(artist_slug, 0) + 1
        return SamplesPage(
            html=page_html,
            resolved_url=f"https://www.whosampled.com/{artist_slug}/samples/",
        )

    page_cache = ParsedSamplesPageCache(capacity=2)
    client = TestClient(app)
    with _override_samples_page(fetch, page_cache=page_cache):
        responses = [
            client.get(f"/artists/{artist_slug}/samples")
            for artist_slug in [
                "Kanye-West",
                "Jay-Z",
                "Kanye-West",
                "Drake",
                "Jay-Z",
            ]
        ]

    assert [response.status_code for response in responses] == [200, 200, 200, 200, 200]
    assert fetches == {"Kanye-West": 1, "Jay-Z": 2, "Drake": 1}


def test_normalized_artist_collection_slugs_share_a_cache_entry() -> None:
    page_html = (FIXTURES / "one_sample_use.html").read_text(encoding="utf-8")
    fetches = 0

    def fetch(artist_slug: str) -> SamplesPage:
        nonlocal fetches
        fetches += 1
        return SamplesPage(
            html=page_html,
            resolved_url="https://www.whosampled.com/Beyonc%C3%A9/samples/",
        )

    client = TestClient(app)
    with _override_samples_page(fetch):
        composed_response = client.get("/artists/Beyonc%C3%A9/samples")
        decomposed_response = client.get("/artists/Beyonce%CC%81/samples")

    assert [composed_response.status_code, decomposed_response.status_code] == [200, 200]
    assert fetches == 1


def test_concurrent_cache_misses_share_one_upstream_fetch() -> None:
    page_html = (FIXTURES / "one_sample_use.html").read_text(encoding="utf-8")
    first_fetch_started = Event()
    release_first_fetch = Event()
    second_cache_lookup = Event()
    clock_lock = Lock()
    clock_reads = 0
    fetches = 0

    def monotonic() -> float:
        nonlocal clock_reads
        with clock_lock:
            clock_reads += 1
            if clock_reads == 2:
                second_cache_lookup.set()
        return 0.0

    def fetch(artist_slug: str) -> SamplesPage:
        nonlocal fetches
        fetches += 1
        if fetches == 1:
            first_fetch_started.set()
            if not release_first_fetch.wait(timeout=2):
                raise TimeoutError("test did not release the first fetch")
        return SamplesPage(
            html=page_html,
            resolved_url="https://www.whosampled.com/Kanye-West/samples/",
        )

    page_cache = ParsedSamplesPageCache(monotonic=monotonic)

    def request_samples() -> tuple[int, dict[str, object]]:
        response = TestClient(app).get("/artists/Kanye-West/samples")
        return response.status_code, response.json()

    with (
        _override_samples_page(fetch, page_cache=page_cache),
        ThreadPoolExecutor(max_workers=2) as pool,
    ):
        first_response = pool.submit(request_samples)
        assert first_fetch_started.wait(timeout=2)
        second_response = pool.submit(request_samples)
        assert second_cache_lookup.wait(timeout=2)
        assert not second_response.done()
        release_first_fetch.set()

        response_results = [first_response.result(timeout=2), second_response.result(timeout=2)]

    assert fetches == 1
    assert response_results[0] == response_results[1]


@pytest.mark.parametrize(
    ("failed_outcome", "expected_status"),
    [
        ("fetch", 502),
        ("challenge", 503),
        ("parse", 502),
    ],
)
def test_unsuccessful_pages_are_not_retained(
    failed_outcome: str,
    expected_status: int,
) -> None:
    valid_page = SamplesPage(
        html=(FIXTURES / "one_sample_use.html").read_text(encoding="utf-8"),
        resolved_url="https://www.whosampled.com/Kanye-West/samples/",
    )
    fetches = 0

    def fetch(artist_slug: str) -> SamplesPage:
        nonlocal fetches
        fetches += 1
        if fetches > 1:
            return valid_page
        if failed_outcome == "fetch":
            raise RuntimeError("upstream fetch failed")
        if failed_outcome == "challenge":
            raise ClearanceFailedError("browserless retry was challenged")
        return SamplesPage(
            html="<html><main></main></html>",
            resolved_url="https://www.whosampled.com/Kanye-West/samples/",
        )

    client = TestClient(app, raise_server_exceptions=False)
    with _override_samples_page(fetch):
        failed_response = client.get("/artists/Kanye-West/samples")
        retry_response = client.get("/artists/Kanye-West/samples")

    assert [failed_response.status_code, retry_response.status_code] == [
        expected_status,
        200,
    ]
    assert fetches == 2


def test_concurrent_callers_share_a_failure_without_caching_it() -> None:
    valid_page = SamplesPage(
        html=(FIXTURES / "one_sample_use.html").read_text(encoding="utf-8"),
        resolved_url="https://www.whosampled.com/Kanye-West/samples/",
    )
    failed_fetch_started = Event()
    release_failed_fetch = Event()
    second_cache_lookup = Event()
    clock_lock = Lock()
    clock_reads = 0
    fetches = 0

    def monotonic() -> float:
        nonlocal clock_reads
        with clock_lock:
            clock_reads += 1
            if clock_reads == 2:
                second_cache_lookup.set()
        return 0.0

    def fetch(artist_slug: str) -> SamplesPage:
        nonlocal fetches
        fetches += 1
        if fetches == 1:
            failed_fetch_started.set()
            if not release_failed_fetch.wait(timeout=2):
                raise TimeoutError("test did not release the failed fetch")
            raise RuntimeError("shared upstream failure")
        return valid_page

    page_cache = ParsedSamplesPageCache(monotonic=monotonic)

    def request_samples() -> tuple[int, dict[str, object]]:
        response = TestClient(app, raise_server_exceptions=False).get(
            "/artists/Kanye-West/samples"
        )
        return response.status_code, response.json()

    with (
        _override_samples_page(fetch, page_cache=page_cache),
        ThreadPoolExecutor(max_workers=2) as pool,
    ):
        first_response = pool.submit(request_samples)
        assert failed_fetch_started.wait(timeout=2)
        second_response = pool.submit(request_samples)
        assert second_cache_lookup.wait(timeout=2)
        release_failed_fetch.set()
        failed_responses = [
            first_response.result(timeout=2),
            second_response.result(timeout=2),
        ]
        retry_response = request_samples()

    assert failed_responses[0] == failed_responses[1]
    assert failed_responses[0][0] == 502
    assert retry_response[0] == 200
    assert fetches == 2


def test_failure_is_published_before_another_caller_can_refetch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    valid_page = SamplesPage(
        html=(FIXTURES / "one_sample_use.html").read_text(encoding="utf-8"),
        resolved_url="https://www.whosampled.com/Kanye-West/samples/",
    )
    reentrant_responses: list[int] = []
    fetches = 0
    client = TestClient(app, raise_server_exceptions=False)

    class PublishingFuture(Future[object]):
        def set_exception(self, exception: BaseException) -> None:
            super().set_exception(exception)
            if not reentrant_responses:
                reentrant_responses.append(
                    client.get("/artists/Kanye-West/samples").status_code
                )

    monkeypatch.setattr("wsmpld.page_cache.Future", PublishingFuture)

    def fetch(artist_slug: str) -> SamplesPage:
        nonlocal fetches
        fetches += 1
        if fetches == 1:
            raise RuntimeError("shared upstream failure")
        return valid_page

    with _override_samples_page(fetch):
        failed_response = client.get("/artists/Kanye-West/samples")

    assert failed_response.status_code == 502
    assert reentrant_responses == [502]
    assert fetches == 1


def test_user_crosses_a_validated_page_boundary_without_an_extra_fetch() -> None:
    first_page = SamplesPage(
        html=(FIXTURES / "live_samples_first_page.html").read_text(encoding="utf-8"),
        resolved_url="https://www.whosampled.com/Kanye-West/samples/",
    )
    final_page_html = (FIXTURES / "live_samples_final_page.html").read_text(
        encoding="utf-8"
    )
    final_page_html = final_page_html.replace("?sp=78", "").replace(
        '<span class="page"><a href="/Kanye-West/samples/">1</a></span>',
        "",
    )
    final_page_html = final_page_html.replace(">78<", ">1<").replace(">79<", ">2<")
    final_page = SamplesPage(
        html=final_page_html,
        resolved_url="https://www.whosampled.com/Kanye-West/samples/?sp=2",
    )
    requested_locations: list[SamplesPageLocation | None] = []

    def fetch(
        artist_slug: str,
        location: SamplesPageLocation | None = None,
    ) -> SamplesPage:
        requested_locations.append(location)
        return first_page if location is None else final_page

    client = TestClient(app)
    with _override_samples_page(fetch):
        first_response = client.get("/artists/Kanye-West/samples?limit=max")
        cursor = first_response.json()["pagination"]["next_cursor"]
        final_response = client.get(
            "/artists/Kanye-West/samples",
            params={"cursor": cursor, "limit": "max"},
        )

    assert first_response.status_code == 200
    assert first_response.json()["items"][0]["sampling_recording"]["title"] == "Bound 2"
    assert isinstance(cursor, str) and cursor
    assert final_response.status_code == 200
    assert final_response.json()["items"][0]["sampling_recording"]["title"] == (
        "Forever La Vida"
    )
    assert final_response.json()["artist"]["samples_url"] == (
        "https://www.whosampled.com/Kanye-West/samples/"
    )
    assert final_response.json()["pagination"] == {
        "next_cursor": None,
        "returned": 1,
        "has_more": False,
    }
    assert [
        location.page_number if location is not None else None
        for location in requested_locations
    ] == [None, 2]


def test_malformed_cursor_is_rejected_before_upstream_work() -> None:
    fetches = 0

    def fetch(
        artist_slug: str,
        location: SamplesPageLocation | None = None,
    ) -> SamplesPage:
        nonlocal fetches
        fetches += 1
        raise AssertionError("upstream fetch must not run for an invalid cursor")

    with _override_samples_page(fetch):
        response = TestClient(app, raise_server_exceptions=False).get(
            "/artists/Kanye-West/samples",
            params={"cursor": "not-a-cursor"},
        )

    assert response.status_code == 400
    assert response.json() == {
        "detail": {
            "code": "invalid_cursor",
            "message": "The Samples cursor is invalid for this request.",
        }
    }
    assert fetches == 0


@pytest.mark.parametrize(
    "cursor",
    [
        _opaque_cursor_payload(
            {"artist": "Kanye-West", "offset": 1, "page": 1, "version": 2}
        ),
        _opaque_cursor_payload(
            {"artist": "Kanye-West", "offset": 1, "page": 1}
        ),
        _opaque_cursor_payload(
            {
                "artist": "Kanye-West",
                "extra": True,
                "offset": 1,
                "page": 1,
                "version": 1,
            }
        ),
        _opaque_cursor_payload(
            {"artist": "Kanye-West", "offset": 0, "page": 1, "version": 1}
        ),
        _opaque_cursor_payload(
            {"artist": "Kanye-West", "offset": -1, "page": 2, "version": 1}
        ),
        _opaque_cursor_payload(
            {"artist": "Kanye-West", "offset": 1, "page": True, "version": 1}
        ),
    ],
)
def test_impossible_or_unsupported_cursor_is_rejected_before_upstream_work(
    cursor: str,
) -> None:
    fetches = 0

    def fetch(
        artist_slug: str,
        location: SamplesPageLocation | None = None,
    ) -> SamplesPage:
        nonlocal fetches
        fetches += 1
        raise AssertionError("upstream fetch must not run for an invalid cursor")

    with _override_samples_page(fetch):
        response = TestClient(app).get(
            "/artists/Kanye-West/samples",
            params={"cursor": cursor},
        )

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "invalid_cursor"
    assert fetches == 0


def test_cursor_for_another_artist_is_rejected_before_upstream_work() -> None:
    page = SamplesPage(
        html=(FIXTURES / "multiple_sample_uses.html").read_text(encoding="utf-8"),
        resolved_url="https://www.whosampled.com/Kanye-West/samples/",
    )
    fetches = 0

    def fetch(
        artist_slug: str,
        location: SamplesPageLocation | None = None,
    ) -> SamplesPage:
        nonlocal fetches
        fetches += 1
        return page

    client = TestClient(app)
    with _override_samples_page(fetch):
        first_response = client.get("/artists/Kanye-West/samples?limit=1")
        cursor = first_response.json()["pagination"]["next_cursor"]
        mismatched_response = client.get(
            "/artists/Jay-Z/samples",
            params={"cursor": cursor},
        )

    assert first_response.status_code == 200
    assert mismatched_response.status_code == 400
    assert mismatched_response.json()["detail"]["code"] == "invalid_cursor"
    assert fetches == 1


def test_live_page_change_that_invalidates_cursor_offset_returns_conflict() -> None:
    original_page = SamplesPage(
        html=(FIXTURES / "multiple_sample_uses.html").read_text(encoding="utf-8"),
        resolved_url="https://www.whosampled.com/Kanye-West/samples/",
    )
    changed_page = SamplesPage(
        html=(FIXTURES / "one_sample_use.html").read_text(encoding="utf-8"),
        resolved_url="https://www.whosampled.com/Kanye-West/samples/",
    )
    fetches = 0

    def fetch(
        artist_slug: str,
        location: SamplesPageLocation | None = None,
    ) -> SamplesPage:
        nonlocal fetches
        fetches += 1
        return original_page if fetches == 1 else changed_page

    client = TestClient(app)
    with _override_samples_page(
        fetch,
        page_cache=ParsedSamplesPageCache(lifetime_seconds=0),
    ):
        first_response = client.get("/artists/Kanye-West/samples?limit=1")
        cursor = first_response.json()["pagination"]["next_cursor"]
        changed_response = client.get(
            "/artists/Kanye-West/samples",
            params={"cursor": cursor},
        )

    assert first_response.status_code == 200
    assert changed_response.status_code == 409
    assert changed_response.json() == {
        "detail": {
            "code": "collection_changed",
            "message": "The live Samples collection changed; restart without a cursor.",
        }
    }
    assert fetches == 2


def test_evicted_page_change_that_invalidates_cursor_offset_returns_conflict() -> None:
    original_html = (FIXTURES / "multiple_sample_uses.html").read_text(encoding="utf-8")
    changed_html = (FIXTURES / "one_sample_use.html").read_text(encoding="utf-8")
    kanye_fetches = 0

    def fetch(artist_slug: str) -> SamplesPage:
        nonlocal kanye_fetches
        if artist_slug == "Kanye-West":
            kanye_fetches += 1
            html = original_html if kanye_fetches == 1 else changed_html
        else:
            html = changed_html
        return SamplesPage(
            html=html,
            resolved_url=f"https://www.whosampled.com/{artist_slug}/samples/",
        )

    client = TestClient(app)
    with _override_samples_page(fetch, page_cache=ParsedSamplesPageCache(capacity=1)):
        first_response = client.get("/artists/Kanye-West/samples?limit=1")
        cursor = first_response.json()["pagination"]["next_cursor"]
        eviction_response = client.get("/artists/Jay-Z/samples")
        changed_response = client.get(
            "/artists/Kanye-West/samples",
            params={"cursor": cursor},
        )

    assert [first_response.status_code, eviction_response.status_code] == [200, 200]
    assert changed_response.status_code == 409
    assert changed_response.json() == {
        "detail": {
            "code": "collection_changed",
            "message": "The live Samples collection changed; restart without a cursor.",
        }
    }
    assert kanye_fetches == 2


def test_internal_continuation_location_fetches_later_samples_page() -> None:
    requested_urls: list[str] = []
    page_html = (FIXTURES / "live_samples_middle_page.html").read_text(encoding="utf-8")

    def fetch_browserlessly(
        url: str, clearance: ClearanceSession, timeout: float
    ) -> BrowserlessResponse:
        requested_urls.append(url)
        return BrowserlessResponse(status_code=200, text=page_html, resolved_url=url)

    page_fetch = _browserless_page(fetch_browserlessly)

    cursor = _opaque_cursor_payload(
        {"artist": "Kanye-West", "offset": 0, "page": 40, "version": 1}
    )
    with _override_samples_page(page_fetch):
        response = TestClient(app).get(
            "/artists/Kanye-West/samples",
            params={"cursor": cursor, "limit": "max"},
        )

    assert response.status_code == 200
    assert requested_urls == ["https://www.whosampled.com/Kanye-West/samples/?sp=40"]
    assert response.json()["artist"]["samples_url"] == (
        "https://www.whosampled.com/Kanye-West/samples/"
    )
    assert response.json()["items"][0]["sampling_recording"]["title"] == (
        "Fight With the Best"
    )


@pytest.mark.parametrize("page_number", [True, 0, 1, -1])
def test_internal_continuation_location_rejects_impossible_pages(
    page_number: int,
) -> None:
    with pytest.raises(ValueError, match="greater than one"):
        SamplesPageLocation(page_number=page_number)


def test_page_fetch_boundary_rejects_arbitrary_url_before_upstream_work() -> None:
    acquisitions = 0
    fetches = 0

    def acquire_clearance(timeout: float) -> ClearanceSession:
        nonlocal acquisitions
        acquisitions += 1
        raise AssertionError("clearance must not run for an arbitrary location")

    def fetch_browserlessly(
        url: str, clearance: ClearanceSession, timeout: float
    ) -> BrowserlessResponse:
        nonlocal fetches
        fetches += 1
        raise AssertionError("fetch must not run for an arbitrary location")

    page_fetch = BrowserlessSamplesPage(
        acquire_clearance=acquire_clearance,
        fetch_browserlessly=fetch_browserlessly,
        monotonic=lambda: 0.0,
    )

    with pytest.raises(TypeError, match="internal Samples page location"):
        page_fetch(  # type: ignore[arg-type]
            "Kanye-West",
            "https://evil.example/Kanye-West/samples/?sp=40",
        )

    assert acquisitions == 0
    assert fetches == 0


@pytest.mark.parametrize(
    "resolved_url",
    [
        "http://www.whosampled.com/Kanye-West/samples/?sp=40",
        "https://evil.example/Kanye-West/samples/?sp=40",
        "https://www.whosampled.com/Jay-Z/samples/?sp=40",
        "https://www.whosampled.com/Kanye-West/?sp=40",
        "https://www.whosampled.com/Kanye-West/samples/?sp=39",
        "https://www.whosampled.com/Kanye-West/samples/?sp=forty",
        "https://www.whosampled.com/Kanye-West/samples/?sp=40&sp=41",
        "https://www.whosampled.com/Kanye-West/samples/?sp=40#again",
    ],
)
def test_unexpected_continuation_redirect_has_stable_bad_gateway_response(
    resolved_url: str,
) -> None:
    page_html = (FIXTURES / "live_samples_middle_page.html").read_text(encoding="utf-8")
    page_fetch = _browserless_page(
        lambda url, clearance, timeout: BrowserlessResponse(
            status_code=200,
            text=page_html,
            resolved_url=resolved_url,
        )
    )

    cursor = _opaque_cursor_payload(
        {"artist": "Kanye-West", "offset": 0, "page": 40, "version": 1}
    )
    with _override_samples_page(page_fetch):
        response = TestClient(app).get(
            "/artists/Kanye-West/samples",
            params={"cursor": cursor, "limit": "max"},
        )

    assert response.status_code == 502
    assert response.json() == {
        "detail": {
            "code": "upstream_invalid",
            "message": "WhoSampled returned an unexpected response.",
        }
    }


def test_forward_redirect_within_artist_samples_collection_is_accepted() -> None:
    middle_page_html = (FIXTURES / "live_samples_middle_page.html").read_text(
        encoding="utf-8"
    )
    track_start = middle_page_html.index('      <section class="trackItem"')
    track_end = middle_page_html.index("      </section>", track_start) + len(
        "      </section>"
    )
    track_markup = middle_page_html[track_start:track_end]
    middle_page_html = (
        middle_page_html[:track_end]
        + "\n"
        + track_markup
        + middle_page_html[track_end:]
    )
    middle_page_html = middle_page_html.replace("?sp=41", "?sp=42").replace(
        '<span class="curr">40</span>',
        '<span class="curr">41</span>',
    )
    middle_page_html = middle_page_html.replace(">41</a>", ">42</a>")
    final_page_html = (FIXTURES / "live_samples_final_page.html").read_text(
        encoding="utf-8"
    ).replace('<span class="curr">79</span>', '<span class="curr">42</span>')
    requested_urls: list[str] = []

    def fetch_browserlessly(
        url: str,
        clearance: ClearanceSession,
        timeout: float,
    ) -> BrowserlessResponse:
        requested_urls.append(url)
        if url.endswith(("?sp=40", "?sp=41")):
            return BrowserlessResponse(
                status_code=200,
                text=middle_page_html,
                resolved_url="https://www.whosampled.com/Kanye-West/samples/?sp=41",
            )
        return BrowserlessResponse(
            status_code=200,
            text=final_page_html,
            resolved_url="https://www.whosampled.com/Kanye-West/samples/?sp=42",
        )

    page_fetch = _browserless_page(fetch_browserlessly)

    cursor = _opaque_cursor_payload(
        {"artist": "Kanye-West", "offset": 0, "page": 40, "version": 1}
    )
    client = TestClient(app)
    with _override_samples_page(page_fetch):
        redirected_response = client.get(
            "/artists/Kanye-West/samples",
            params={"cursor": cursor, "limit": 1},
        )
        same_page_cursor = redirected_response.json()["pagination"]["next_cursor"]
        cached_response = client.get(
            "/artists/Kanye-West/samples",
            params={"cursor": same_page_cursor, "limit": "max"},
        )
        next_cursor = cached_response.json()["pagination"]["next_cursor"]
        final_response = client.get(
            "/artists/Kanye-West/samples",
            params={"cursor": next_cursor, "limit": "max"},
        )

    assert redirected_response.status_code == 200
    assert cached_response.status_code == 200
    assert redirected_response.json()["artist"]["samples_url"] == (
        "https://www.whosampled.com/Kanye-West/samples/"
    )
    assert final_response.status_code == 200
    assert final_response.json()["pagination"]["next_cursor"] is None
    assert requested_urls == [
        "https://www.whosampled.com/Kanye-West/samples/?sp=40",
        "https://www.whosampled.com/Kanye-West/samples/?sp=42",
    ]


def test_initial_page_redirect_to_another_artist_has_stable_bad_gateway_response() -> None:
    page_html = (FIXTURES / "live_samples_first_page.html").read_text(encoding="utf-8")
    page_fetch = _browserless_page(
        lambda url, clearance, timeout: BrowserlessResponse(
            status_code=200,
            text=page_html,
            resolved_url="https://www.whosampled.com/Jay-Z/samples/",
        )
    )

    with _override_samples_page(page_fetch):
        response = TestClient(app).get("/artists/Kanye-West/samples?limit=max")

    assert response.status_code == 502
    assert response.json()["detail"]["code"] == "upstream_invalid"


def test_browserless_redirect_loop_has_stable_bad_gateway_response() -> None:
    def redirect_loop(
        url: str, clearance: ClearanceSession, timeout: float
    ) -> BrowserlessResponse:
        raise TooManyRedirects("Too many redirects")

    page_fetch = _browserless_page(redirect_loop)

    with _override_samples_page(page_fetch):
        response = TestClient(app).get("/artists/Kanye-West/samples")

    assert response.status_code == 502
    assert response.json()["detail"]["code"] == "upstream_invalid"


@pytest.mark.parametrize(
    ("status_code", "response_html"),
    [
        (404, "artist not found"),
        (403, "<title>Just a moment...</title>"),
    ],
)
def test_unsafe_destination_is_rejected_before_status_or_challenge_mapping(
    status_code: int,
    response_html: str,
) -> None:
    acquisitions = 0
    fetches = 0

    def acquire_clearance(timeout: float) -> ClearanceSession:
        nonlocal acquisitions
        acquisitions += 1
        return ClearanceSession(
            cookies={"cf_clearance": "secret"},
            user_agent="test-agent",
            expires_at=10_000.0,
        )

    def fetch_browserlessly(
        url: str, clearance: ClearanceSession, timeout: float
    ) -> BrowserlessResponse:
        nonlocal fetches
        fetches += 1
        return BrowserlessResponse(
            status_code=status_code,
            text=response_html,
            resolved_url="https://evil.example/Kanye-West/samples/",
        )

    page_fetch = BrowserlessSamplesPage(
        acquire_clearance=acquire_clearance,
        fetch_browserlessly=fetch_browserlessly,
        monotonic=lambda: 0.0,
    )

    with _override_samples_page(page_fetch):
        response = TestClient(app).get("/artists/Kanye-West/samples")

    assert response.status_code == 502
    assert response.json()["detail"]["code"] == "upstream_invalid"
    assert acquisitions == 1
    assert fetches == 1


def test_unsafe_artist_slugs_receive_normal_validation_errors() -> None:
    client = TestClient(app)
    unsafe_slugs = ["%2F", "%5C", "%00", "%2E", "%2E%2E", "%20%20", "a" * 201]

    for artist_slug in unsafe_slugs:
        response = client.get(f"/artists/{artist_slug}/samples")

        assert response.status_code == 422, (artist_slug, response.text)
        assert response.json()["detail"][0]["type"] in {
            "string_too_long",
            "value_error",
        }


def test_valid_punctuation_and_unicode_slug_is_preserved() -> None:
    requested_slug = "Björk!$&'()+,;=@"
    encoded_slug = quote(requested_slug, safe="")
    received_slugs: list[str] = []
    page = SamplesPage(
        html=(FIXTURES / "one_sample_use.html").read_text(encoding="utf-8"),
        resolved_url=f"https://www.whosampled.com/{encoded_slug}/samples/",
    )

    def fetch(artist_slug: str) -> SamplesPage:
        received_slugs.append(artist_slug)
        return page

    with _override_samples_page(fetch):
        response = TestClient(app).get(f"/artists/{encoded_slug}/samples")

    assert response.status_code == 200
    assert response.json()["artist"]["requested_slug"] == requested_slug
    assert received_slugs == [requested_slug]


def test_generated_docs_describe_the_samples_contract() -> None:
    schema = TestClient(app).get("/openapi.json").json()
    operation = schema["paths"]["/artists/{artist_slug}/samples"]["get"]

    assert operation["summary"] == "Get an artist's Samples"
    assert [(parameter["name"], parameter["in"]) for parameter in operation["parameters"]] == [
        ("artist_slug", "path"),
        ("limit", "query"),
        ("cursor", "query"),
    ]
    assert "live Samples collection" in operation["description"]
    assert "at most one upstream page" in operation["description"]
    assert operation["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/SamplesResponse"
    }
    assert operation["responses"]["404"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ErrorResponse"
    }
    assert operation["responses"]["400"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ErrorResponse"
    }
    assert operation["responses"]["409"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ErrorResponse"
    }
    assert operation["responses"]["502"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ErrorResponse"
    }
    assert operation["responses"]["503"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ErrorResponse"
    }
    assert operation["responses"]["504"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ErrorResponse"
    }
    assert schema["components"]["schemas"]["ErrorDetail"]["properties"]["code"] == {
        "type": "string",
        "enum": [
            "artist_not_found",
            "upstream_invalid",
            "clearance_failed",
            "lookup_timeout",
            "invalid_cursor",
            "collection_changed",
            "upstream_rate_limited",
        ],
        "title": "Code",
    }
    pagination_schema = schema["components"]["schemas"]["Pagination"]
    assert "source_page" not in pagination_schema["properties"]
    assert pagination_schema["properties"]["next_cursor"] == {
        "anyOf": [{"type": "string"}, {"type": "null"}],
        "title": "Next Cursor",
    }
    assert {
        status: operation["responses"][status]["content"]["application/json"]["example"]
        for status in ("400", "404", "409", "502", "504")
    } == {
        "400": {
            "detail": {
                "code": "invalid_cursor",
                "message": "The Samples cursor is invalid for this request.",
            }
        },
        "404": {
            "detail": {"code": "artist_not_found", "message": "Artist was not found."}
        },
        "409": {
            "detail": {
                "code": "collection_changed",
                "message": "The live Samples collection changed; restart without a cursor.",
            }
        },
        "502": {
            "detail": {
                "code": "upstream_invalid",
                "message": "WhoSampled returned an unexpected response.",
            }
        },
        "504": {
            "detail": {
                "code": "lookup_timeout",
                "message": "The lookup exceeded its 120-second time limit.",
            }
        },
    }
    assert operation["responses"]["503"]["headers"]["Retry-After"] == {
        "description": "Validated delay or HTTP date supplied by WhoSampled after a rate limit.",
        "schema": {"type": "string"},
    }
    assert operation["responses"]["503"]["content"]["application/json"]["examples"] == {
        "clearance_failed": {
            "summary": "Clearance acquisition failed",
            "value": {
                "detail": {
                    "code": "clearance_failed",
                    "message": "Could not acquire a reusable upstream session.",
                }
            },
        },
        "upstream_rate_limited": {
            "summary": "WhoSampled rate limited the request",
            "value": {
                "detail": {
                    "code": "upstream_rate_limited",
                    "message": "WhoSampled rate limited the request.",
                }
            },
        },
    }


def test_user_can_request_every_sample_use_on_the_current_page() -> None:
    page = SamplesPage(
        html=(FIXTURES / "multiple_sample_uses.html").read_text(encoding="utf-8"),
        resolved_url="https://www.whosampled.com/Kanye-West/samples/",
    )
    with _override_samples_page(lambda artist_slug: page):
        response = TestClient(app).get("/artists/Kanye-West/samples?limit=max")

    assert response.status_code == 200
    assert [
        item["sampling_recording"]["title"] for item in response.json()["items"]
    ] == ["Famous", "Power"]
    assert response.json()["pagination"] == {
        "next_cursor": None,
        "returned": 2,
        "has_more": False,
    }


def test_2pac_samples_use_page_artist_when_live_track_credit_is_implicit() -> None:
    page = SamplesPage(
        html=(FIXTURES / "live_implicit_artist_credit.html").read_text(encoding="utf-8"),
        resolved_url="https://www.whosampled.com/2Pac/samples/",
    )
    with _override_samples_page(lambda artist_slug: page):
        response = TestClient(app).get("/artists/2Pac/samples?limit=max")

    assert response.status_code == 200
    assert response.json() == {
        "artist": {
            "requested_slug": "2Pac",
            "name": "2Pac",
            "samples_url": "https://www.whosampled.com/2Pac/samples/",
        },
        "items": [
            {
                "sampling_recording": {
                    "title": "Example Track",
                    "artist_credit": "2Pac",
                    "year": 1996,
                    "producer_credit": None,
                    "url": "https://www.whosampled.com/2Pac/Example-Track/",
                },
                "source_recording": {
                    "title": "Example Source",
                    "artist_credit": "Source Artist",
                    "year": 1985,
                    "url": "https://www.whosampled.com/sample/8/example-source/",
                },
            }
        ],
        "pagination": {"next_cursor": None, "returned": 1, "has_more": False},
    }


def test_live_track_explicit_artist_credit_remains_unchanged() -> None:
    page = SamplesPage(
        html=(FIXTURES / "live_sample_uses.html").read_text(encoding="utf-8"),
        resolved_url="https://www.whosampled.com/Example-Artist/samples/",
    )
    with _override_samples_page(lambda artist_slug: page):
        response = TestClient(app).get("/artists/Example-Artist/samples?limit=max")

    assert response.status_code == 200
    assert response.json()["items"][0]["sampling_recording"]["artist_credit"] == (
        "Example Artist feat. Guest Artist"
    )


def test_incomplete_implicit_credit_sample_use_still_fails_closed() -> None:
    document = (FIXTURES / "live_implicit_artist_credit.html").read_text(encoding="utf-8")
    document = document.replace('class="connectionName"', 'class="unknown-connection"')
    page = SamplesPage(
        html=document,
        resolved_url="https://www.whosampled.com/2Pac/samples/",
    )
    with _override_samples_page(lambda artist_slug: page):
        response = TestClient(app).get("/artists/2Pac/samples?limit=max")

    assert response.status_code == 502
    assert response.json() == {
        "detail": {
            "code": "upstream_invalid",
            "message": "WhoSampled returned an unexpected response.",
        }
    }


def test_positive_numeric_limit_applies_to_current_page_sample_uses() -> None:
    page = SamplesPage(
        html=(FIXTURES / "multiple_sample_uses.html").read_text(encoding="utf-8"),
        resolved_url="https://www.whosampled.com/Kanye-West/samples/",
    )
    client = TestClient(app)

    with _override_samples_page(lambda artist_slug: page):
        limited_response = client.get("/artists/Kanye-West/samples?limit=1")
        oversized_response = client.get("/artists/Kanye-West/samples?limit=20")

    assert limited_response.status_code == 200
    assert [
        item["sampling_recording"]["title"] for item in limited_response.json()["items"]
    ] == ["Famous"]
    limited_pagination = limited_response.json()["pagination"]
    assert isinstance(limited_pagination["next_cursor"], str)
    assert limited_pagination["returned"] == 1
    assert limited_pagination["has_more"] is True
    assert oversized_response.status_code == 200
    assert len(oversized_response.json()["items"]) == 2
    assert oversized_response.json()["pagination"] == {
        "next_cursor": None,
        "returned": 2,
        "has_more": False,
    }


def test_limit_applies_after_display_groups_are_flattened() -> None:
    page = SamplesPage(
        html=(FIXTURES / "grouped_sample_uses.html").read_text(encoding="utf-8"),
        resolved_url="https://www.whosampled.com/Example-Artist/samples/",
    )
    with _override_samples_page(lambda artist_slug: page):
        response = TestClient(app).get("/artists/Example-Artist/samples?limit=2")

    assert response.status_code == 200
    assert [item["source_recording"]["title"] for item in response.json()["items"]] == [
        "First Source",
        "Second Source",
    ]
    pagination = response.json()["pagination"]
    assert isinstance(pagination["next_cursor"], str)
    assert pagination["returned"] == 2
    assert pagination["has_more"] is True


def test_invalid_limits_receive_normal_validation_errors() -> None:
    client = TestClient(app)

    for limit in ["0", "-1", "1.5", "", "MAX", "all"]:
        response = client.get(f"/artists/Kanye-West/samples?limit={limit}")

        assert response.status_code == 422, (limit, response.text)
        assert isinstance(response.json()["detail"], list)


def test_definitive_missing_artist_has_stable_not_found_response() -> None:
    def missing_artist(artist_slug: str) -> SamplesPage:
        raise ArtistNotFoundError(artist_slug)

    with _override_samples_page(missing_artist):
        response = TestClient(app).get("/artists/Definitely-Missing/samples")

    assert response.status_code == 404
    assert response.json() == {
        "detail": {
            "code": "artist_not_found",
            "message": "Artist was not found.",
        }
    }


def test_malformed_upstream_html_has_stable_bad_gateway_response() -> None:
    page = SamplesPage(
        html=(
            "<html><body><main data-artist-name='Kanye West'>"
            "<article class='sample-use'></article></main></body></html>"
        ),
        resolved_url="https://www.whosampled.com/Kanye-West/samples/",
    )
    with _override_samples_page(lambda artist_slug: page):
        response = TestClient(app, raise_server_exceptions=False).get(
            "/artists/Kanye-West/samples"
        )

    assert response.status_code == 502
    assert response.json() == {
        "detail": {
            "code": "upstream_invalid",
            "message": "WhoSampled returned an unexpected response.",
        }
    }


def test_unexpected_upstream_failure_has_stable_bad_gateway_response() -> None:
    def failed_fetch(artist_slug: str) -> SamplesPage:
        raise RuntimeError(artist_slug)

    with _override_samples_page(failed_fetch):
        response = TestClient(app, raise_server_exceptions=False).get(
            "/artists/Kanye-West/samples"
        )

    assert response.status_code == 502
    assert response.json() == {
        "detail": {
            "code": "upstream_invalid",
            "message": "WhoSampled returned an unexpected response.",
        }
    }


def test_clearance_failure_has_stable_service_unavailable_response() -> None:
    def failed_clearance(artist_slug: str) -> SamplesPage:
        raise ClearanceFailedError(artist_slug)

    with _override_samples_page(failed_clearance):
        response = TestClient(app, raise_server_exceptions=False).get(
            "/artists/Kanye-West/samples"
        )

    assert response.status_code == 503
    assert response.json() == {
        "detail": {
            "code": "clearance_failed",
            "message": "Could not acquire a reusable upstream session.",
        }
    }


def test_complete_lookup_timeout_has_stable_gateway_timeout_response(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def timed_out_lookup(artist_slug: str) -> SamplesPage:
        raise LookupTimeoutError(artist_slug)

    with caplog.at_level("INFO"), _override_samples_page(timed_out_lookup):
        response = TestClient(app, raise_server_exceptions=False).get(
            "/artists/Kanye-West/samples"
        )

    assert response.status_code == 504
    assert response.json() == {
        "detail": {
            "code": "lookup_timeout",
            "message": "The lookup exceeded its 120-second time limit.",
        }
    }
    assert "Samples lookup timed out artist_slug=Kanye-West" in [
        record.getMessage() for record in caplog.records
    ]


def test_clearance_is_acquired_lazily_on_first_accepted_lookup() -> None:
    events: list[str] = []
    page_html = (FIXTURES / "one_sample_use.html").read_text(encoding="utf-8")

    def acquire_clearance(timeout: float) -> ClearanceSession:
        events.append(f"acquire:{timeout}")
        return ClearanceSession(
            cookies={"cf_clearance": "secret"},
            user_agent="test-agent",
            expires_at=10_000.0,
        )

    def fetch_browserlessly(
        url: str, clearance: ClearanceSession, timeout: float
    ) -> BrowserlessResponse:
        events.append(f"fetch:{url}:{clearance.user_agent}:{timeout}")
        return BrowserlessResponse(
            status_code=200,
            text=page_html,
            resolved_url=url,
        )

    fetch_samples_page = BrowserlessSamplesPage(
        acquire_clearance=acquire_clearance,
        fetch_browserlessly=fetch_browserlessly,
        monotonic=lambda: 0.0,
    )

    assert events == []

    with _override_samples_page(fetch_samples_page):
        response = TestClient(app).get("/artists/Kanye-West/samples")

    assert response.status_code == 200
    assert events == [
        "acquire:90.0",
        "fetch:https://www.whosampled.com/Kanye-West/samples/:test-agent:20.0",
    ]


def test_sequential_requests_reuse_unexpired_clearance(
    caplog: pytest.LogCaptureFixture,
) -> None:
    page_html = (FIXTURES / "one_sample_use.html").read_text(encoding="utf-8")
    acquisitions = 0
    fetches = 0
    current_time = 0.0
    sleeps: list[float] = []

    def acquire_clearance(timeout: float) -> ClearanceSession:
        nonlocal acquisitions
        acquisitions += 1
        return ClearanceSession(
            cookies={"cf_clearance": "do-not-log-this-secret"},
            user_agent="test-agent",
            expires_at=10_000.0,
        )

    def fetch_browserlessly(
        url: str, clearance: ClearanceSession, timeout: float
    ) -> BrowserlessResponse:
        nonlocal fetches
        fetches += 1
        return BrowserlessResponse(status_code=200, text=page_html, resolved_url=url)

    def sleep(delay: float) -> None:
        nonlocal current_time
        sleeps.append(delay)
        current_time += delay

    fetch_samples_page = BrowserlessSamplesPage(
        acquire_clearance=acquire_clearance,
        fetch_browserlessly=fetch_browserlessly,
        monotonic=lambda: current_time,
        sleep=sleep,
        jitter=lambda lower, upper: 0.0,
    )

    with caplog.at_level("INFO"), _override_samples_page(
        fetch_samples_page,
        page_cache=ParsedSamplesPageCache(lifetime_seconds=0),
    ):
        responses = [
            TestClient(app).get("/artists/Kanye-West/samples")
            for _ in range(2)
        ]

    assert [response.status_code for response in responses] == [200, 200]
    assert all(
        response.json()["artist"]["requested_slug"] == "Kanye-West"
        for response in responses
    )
    assert acquisitions == 1
    assert fetches == 2
    messages = [record.getMessage() for record in caplog.records]
    assert messages.count("reusing unexpired clearance session") == 1
    assert "do-not-log-this-secret" not in "\n".join(messages)
    assert "do-not-log-this-secret" not in "\n".join(response.text for response in responses)


def test_expired_clearance_is_discarded_before_next_request(
    caplog: pytest.LogCaptureFixture,
) -> None:
    page_html = (FIXTURES / "one_sample_use.html").read_text(encoding="utf-8")
    current_time = 0.0
    acquisitions = 0
    fetches = 0

    def acquire_clearance(timeout: float) -> ClearanceSession:
        nonlocal acquisitions
        acquisitions += 1
        return ClearanceSession(
            cookies={"cf_clearance": f"do-not-log-secret-{acquisitions}"},
            user_agent=f"test-agent-{acquisitions}",
            expires_at=current_time + 10.0,
        )

    def fetch_browserlessly(
        url: str, clearance: ClearanceSession, timeout: float
    ) -> BrowserlessResponse:
        nonlocal fetches
        fetches += 1
        return BrowserlessResponse(status_code=200, text=page_html, resolved_url=url)

    fetch_samples_page = BrowserlessSamplesPage(
        acquire_clearance=acquire_clearance,
        fetch_browserlessly=fetch_browserlessly,
        monotonic=lambda: current_time,
    )

    with caplog.at_level("INFO"), _override_samples_page(
        fetch_samples_page,
        page_cache=ParsedSamplesPageCache(lifetime_seconds=0),
    ):
        first_response = TestClient(app).get("/artists/Kanye-West/samples")
        current_time = 10.0
        second_response = TestClient(app).get("/artists/Kanye-West/samples")

    assert [first_response.status_code, second_response.status_code] == [200, 200]
    assert acquisitions == 2
    assert fetches == 2
    messages = [record.getMessage() for record in caplog.records]
    assert messages.count("discarding expired clearance session") == 1
    assert "do-not-log-secret" not in "\n".join(messages)
    assert "do-not-log-secret" not in first_response.text + second_response.text


def test_challenged_browserless_fetch_refreshes_clearance_once_and_retries(
    caplog: pytest.LogCaptureFixture,
) -> None:
    acquisitions = 0
    fetches = 0
    current_time = 0.0
    fetch_times: list[float] = []
    sleeps: list[float] = []
    events: list[str] = []
    page_html = (FIXTURES / "one_sample_use.html").read_text(encoding="utf-8")

    def acquire_clearance(timeout: float) -> ClearanceSession:
        nonlocal acquisitions
        acquisitions += 1
        events.append("acquire")
        return ClearanceSession(
            cookies={"cf_clearance": f"secret-{acquisitions}"},
            user_agent=f"test-agent-{acquisitions}",
            expires_at=10_000.0,
        )

    def fetch_browserlessly(
        url: str, clearance: ClearanceSession, timeout: float
    ) -> BrowserlessResponse:
        nonlocal fetches
        fetches += 1
        events.append("fetch")
        fetch_times.append(current_time)
        if fetches == 1:
            return BrowserlessResponse(
                status_code=200,
                text="<title>Just a moment...</title>",
                resolved_url=url,
            )
        return BrowserlessResponse(status_code=200, text=page_html, resolved_url=url)

    def sleep(delay: float) -> None:
        nonlocal current_time
        events.append("pace")
        sleeps.append(delay)
        current_time += delay

    fetch_samples_page = BrowserlessSamplesPage(
        acquire_clearance=acquire_clearance,
        fetch_browserlessly=fetch_browserlessly,
        monotonic=lambda: current_time,
        sleep=sleep,
        jitter=lambda lower, upper: 0.5,
        minimum_interval_seconds=2.0,
        minimum_jitter_seconds=0.25,
        maximum_jitter_seconds=0.75,
    )

    with caplog.at_level("INFO"), _override_samples_page(fetch_samples_page):
        response = TestClient(app).get("/artists/Kanye-West/samples")

    assert response.status_code == 200
    assert acquisitions == 2
    assert fetches == 2
    assert fetch_times == [0.0, 2.5]
    assert sleeps == [2.5]
    assert events == ["acquire", "fetch", "acquire", "pace", "fetch"]
    messages = [record.getMessage() for record in caplog.records]
    assert messages.count("browserless Samples fetch challenged; refreshing clearance") == 1
    assert "secret-" not in "\n".join(messages)


def test_second_browserless_challenge_fails_without_browser_fallback() -> None:
    acquisitions = 0
    fetches = 0

    def acquire_clearance(timeout: float) -> ClearanceSession:
        nonlocal acquisitions
        acquisitions += 1
        return ClearanceSession(
            cookies={"cf_clearance": f"secret-{acquisitions}"},
            user_agent=f"test-agent-{acquisitions}",
            expires_at=10_000.0,
        )

    def fetch_browserlessly(
        url: str, clearance: ClearanceSession, timeout: float
    ) -> BrowserlessResponse:
        nonlocal fetches
        fetches += 1
        return BrowserlessResponse(
            status_code=403,
            text="<title>Just a moment...</title>",
            resolved_url=url,
        )

    fetch_samples_page = BrowserlessSamplesPage(
        acquire_clearance=acquire_clearance,
        fetch_browserlessly=fetch_browserlessly,
        monotonic=lambda: 0.0,
        sleep=lambda delay: None,
        minimum_interval_seconds=0.0,
        minimum_jitter_seconds=0.0,
        maximum_jitter_seconds=0.0,
    )

    with _override_samples_page(fetch_samples_page):
        response = TestClient(app).get("/artists/Kanye-West/samples")

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "clearance_failed"
    assert acquisitions == 2
    assert fetches == 2


def test_challenge_retry_pacing_stops_at_the_complete_lookup_deadline() -> None:
    current_time = 0.0
    sleeps: list[float] = []
    fetches = 0

    def sleep(delay: float) -> None:
        nonlocal current_time
        sleeps.append(delay)
        current_time += delay

    def fetch_browserlessly(
        url: str, clearance: ClearanceSession, timeout: float
    ) -> BrowserlessResponse:
        nonlocal fetches
        fetches += 1
        if fetches > 1:
            raise AssertionError("retry must not start after the complete deadline")
        return BrowserlessResponse(
            status_code=403,
            text="<title>Just a moment...</title>",
            resolved_url=url,
        )

    fetch_samples_page = BrowserlessSamplesPage(
        acquire_clearance=lambda timeout: ClearanceSession(
            cookies={"cf_clearance": "secret"},
            user_agent="test-agent",
            expires_at=10_000.0,
        ),
        fetch_browserlessly=fetch_browserlessly,
        monotonic=lambda: current_time,
        sleep=sleep,
        jitter=lambda lower, upper: 0.0,
        minimum_interval_seconds=121.0,
        minimum_jitter_seconds=0.0,
        maximum_jitter_seconds=0.0,
    )

    with _override_samples_page(fetch_samples_page):
        response = TestClient(app).get("/artists/Kanye-West/samples")

    assert response.status_code == 504
    assert response.json()["detail"]["code"] == "lookup_timeout"
    assert fetches == 1
    assert sleeps == [120.0]


def test_clearance_acquisition_exception_has_stable_service_unavailable_response() -> None:
    fetches = 0

    def acquire_clearance(timeout: float) -> ClearanceSession:
        raise RuntimeError(f"acquisition failed after {timeout} seconds")

    def fetch_browserlessly(
        url: str, clearance: ClearanceSession, timeout: float
    ) -> BrowserlessResponse:
        nonlocal fetches
        fetches += 1
        raise AssertionError("browserless fetch must not run without clearance")

    fetch_samples_page = BrowserlessSamplesPage(
        acquire_clearance=acquire_clearance,
        fetch_browserlessly=fetch_browserlessly,
        monotonic=lambda: 0.0,
    )

    with _override_samples_page(fetch_samples_page):
        response = TestClient(app).get("/artists/Kanye-West/samples")

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "clearance_failed"
    assert fetches == 0


def test_complete_operation_budget_stops_work_before_browserless_fetch() -> None:
    acquisition_completed = False
    fetches = 0

    def acquire_clearance(timeout: float) -> ClearanceSession:
        nonlocal acquisition_completed
        acquisition_completed = True
        return ClearanceSession(
            cookies={"cf_clearance": "secret"},
            user_agent="test-agent",
            expires_at=10_000.0,
        )

    def fetch_browserlessly(
        url: str, clearance: ClearanceSession, timeout: float
    ) -> BrowserlessResponse:
        nonlocal fetches
        fetches += 1
        raise AssertionError("fetch must not start after the complete deadline")

    fetch_samples_page = BrowserlessSamplesPage(
        acquire_clearance=acquire_clearance,
        fetch_browserlessly=fetch_browserlessly,
        monotonic=lambda: 121.0 if acquisition_completed else 0.0,
    )

    with _override_samples_page(fetch_samples_page):
        response = TestClient(app).get("/artists/Kanye-West/samples")

    assert response.status_code == 504
    assert response.json()["detail"]["code"] == "lookup_timeout"
    assert fetches == 0


def test_complete_operation_budget_expires_before_clearance_acquisition() -> None:
    clock_reads = 0
    acquisitions = 0

    def monotonic() -> float:
        nonlocal clock_reads
        clock_reads += 1
        return 121.0 if clock_reads >= 3 else 0.0

    def acquire_clearance(timeout: float) -> ClearanceSession:
        nonlocal acquisitions
        acquisitions += 1
        raise AssertionError("acquisition must not start after the complete deadline")

    fetch_samples_page = BrowserlessSamplesPage(
        acquire_clearance=acquire_clearance,
        fetch_browserlessly=lambda url, clearance, timeout: BrowserlessResponse(
            status_code=500,
            text="",
            resolved_url=url,
        ),
        monotonic=monotonic,
    )

    with _override_samples_page(fetch_samples_page):
        response = TestClient(app).get("/artists/Kanye-West/samples")

    assert response.status_code == 504
    assert response.json()["detail"]["code"] == "lookup_timeout"
    assert acquisitions == 0


def test_clearance_timeout_at_complete_deadline_is_lookup_timeout() -> None:
    deadline_expired = False

    def acquire_clearance(timeout: float) -> ClearanceSession:
        nonlocal deadline_expired
        deadline_expired = True
        raise TimeoutError(f"clearance timed out after {timeout} seconds")

    fetch_samples_page = BrowserlessSamplesPage(
        acquire_clearance=acquire_clearance,
        fetch_browserlessly=lambda url, clearance, timeout: BrowserlessResponse(
            status_code=500,
            text="",
            resolved_url=url,
        ),
        monotonic=lambda: 121.0 if deadline_expired else 0.0,
    )

    with _override_samples_page(fetch_samples_page):
        response = TestClient(app).get("/artists/Kanye-West/samples")

    assert response.status_code == 504
    assert response.json()["detail"]["code"] == "lookup_timeout"


def test_browserless_timeout_at_complete_deadline_is_lookup_timeout() -> None:
    deadline_expired = False

    def fetch_browserlessly(
        url: str, clearance: ClearanceSession, timeout: float
    ) -> BrowserlessResponse:
        nonlocal deadline_expired
        deadline_expired = True
        raise TimeoutError(f"fetch timed out after {timeout} seconds")

    fetch_samples_page = BrowserlessSamplesPage(
        acquire_clearance=lambda timeout: ClearanceSession(
            cookies={"cf_clearance": "secret"},
            user_agent="test-agent",
            expires_at=10_000.0,
        ),
        fetch_browserlessly=fetch_browserlessly,
        monotonic=lambda: 121.0 if deadline_expired else 0.0,
    )

    with _override_samples_page(fetch_samples_page):
        response = TestClient(app).get("/artists/Kanye-West/samples")

    assert response.status_code == 504
    assert response.json()["detail"]["code"] == "lookup_timeout"


def test_individual_browserless_timeout_before_complete_deadline_is_upstream_invalid() -> None:
    def fetch_browserlessly(
        url: str, clearance: ClearanceSession, timeout: float
    ) -> BrowserlessResponse:
        raise TimeoutError(f"fetch timed out after {timeout} seconds")

    fetch_samples_page = BrowserlessSamplesPage(
        acquire_clearance=lambda timeout: ClearanceSession(
            cookies={"cf_clearance": "secret"},
            user_agent="test-agent",
            expires_at=10_000.0,
        ),
        fetch_browserlessly=fetch_browserlessly,
        monotonic=lambda: 0.0,
    )

    with _override_samples_page(fetch_samples_page):
        response = TestClient(app).get("/artists/Kanye-West/samples")

    assert response.status_code == 502
    assert response.json()["detail"]["code"] == "upstream_invalid"


def test_concurrent_requests_serialize_browserless_fetches() -> None:
    page_html = (FIXTURES / "one_sample_use.html").read_text(encoding="utf-8")
    release_first_fetch = Event()
    first_fetch_started = Event()
    state_lock = Lock()
    acquisitions = 0
    fetches = 0
    current_time = 0.0
    sleeps: list[float] = []

    def acquire_clearance(timeout: float) -> ClearanceSession:
        nonlocal acquisitions
        acquisitions += 1
        return ClearanceSession(
            cookies={"cf_clearance": "secret"},
            user_agent="test-agent",
            expires_at=10_000.0,
        )

    def fetch_browserlessly(
        url: str, clearance: ClearanceSession, timeout: float
    ) -> BrowserlessResponse:
        nonlocal fetches
        with state_lock:
            fetches += 1
            fetch_number = fetches
        if fetch_number == 1:
            first_fetch_started.set()
            if not release_first_fetch.wait(timeout=5):
                raise TimeoutError("test did not release the first fetch")
        return BrowserlessResponse(status_code=200, text=page_html, resolved_url=url)

    def sleep(delay: float) -> None:
        nonlocal current_time
        sleeps.append(delay)
        current_time += delay

    fetch_samples_page = BrowserlessSamplesPage(
        acquire_clearance=acquire_clearance,
        fetch_browserlessly=fetch_browserlessly,
        monotonic=lambda: current_time,
        sleep=sleep,
        jitter=lambda lower, upper: 0.0,
    )

    def request_samples(artist_slug: str) -> int:
        return TestClient(app).get(f"/artists/{artist_slug}/samples").status_code

    with _override_samples_page(fetch_samples_page), ThreadPoolExecutor(max_workers=2) as pool:
        first_response = pool.submit(request_samples, "Kanye-West")
        assert first_fetch_started.wait(timeout=2)
        second_response = pool.submit(request_samples, "Jay-Z")
        try:
            with pytest.raises(FutureTimeoutError):
                second_response.result(timeout=0.5)
        finally:
            release_first_fetch.set()

        assert first_response.result(timeout=2) == 200
        assert second_response.result(timeout=2) == 200

    assert acquisitions == 1
    assert fetches == 2
    assert sleeps == [4.0]


def test_lifecycle_logs_report_acquisition_browserless_fetch_and_parse_count(
    caplog: pytest.LogCaptureFixture,
) -> None:
    page_html = (FIXTURES / "one_sample_use.html").read_text(encoding="utf-8")

    def acquire_clearance(timeout: float) -> ClearanceSession:
        return ClearanceSession(
            cookies={"cf_clearance": "do-not-log-this-secret"},
            user_agent="test-agent",
            expires_at=10_000.0,
        )

    def fetch_browserlessly(
        url: str, clearance: ClearanceSession, timeout: float
    ) -> BrowserlessResponse:
        return BrowserlessResponse(status_code=200, text=page_html, resolved_url=url)

    fetch_samples_page = BrowserlessSamplesPage(
        acquire_clearance=acquire_clearance,
        fetch_browserlessly=fetch_browserlessly,
        monotonic=lambda: 0.0,
    )

    with caplog.at_level("INFO"), _override_samples_page(fetch_samples_page):
        response = TestClient(app).get("/artists/Kanye-West/samples")

    assert response.status_code == 200
    messages = [record.getMessage() for record in caplog.records]
    assert any("clearance acquisition started" in message for message in messages)
    assert any("browserless Samples fetch started" in message for message in messages)
    assert any("parsed 1 Sample Uses" in message for message in messages)
    combined = "\n".join(messages)
    assert "do-not-log-this-secret" not in combined
    assert page_html not in combined


def test_existing_artist_without_sample_uses_has_empty_success_response() -> None:
    page = SamplesPage(
        html=(FIXTURES / "empty_sample_uses.html").read_text(encoding="utf-8"),
        resolved_url="https://www.whosampled.com/No-Samples-Artist/samples/",
    )
    with _override_samples_page(lambda artist_slug: page):
        response = TestClient(app).get("/artists/No-Samples-Artist/samples?limit=max")

    assert response.status_code == 200
    assert response.json() == {
        "artist": {
            "requested_slug": "No-Samples-Artist",
            "name": "No Samples Artist",
            "samples_url": "https://www.whosampled.com/No-Samples-Artist/samples/",
        },
        "items": [],
        "pagination": {"next_cursor": None, "returned": 0, "has_more": False},
    }


def test_invalid_resolved_upstream_url_has_stable_bad_gateway_response() -> None:
    page = SamplesPage(
        html=(FIXTURES / "one_sample_use.html").read_text(encoding="utf-8"),
        resolved_url="https://evil.example/Kanye-West/samples/",
    )
    fetches = 0

    def fetch(artist_slug: str) -> SamplesPage:
        nonlocal fetches
        fetches += 1
        return page

    with _override_samples_page(fetch):
        responses = [
            TestClient(app, raise_server_exceptions=False).get(
                "/artists/Kanye-West/samples"
            )
            for _ in range(2)
        ]

    assert [response.status_code for response in responses] == [502, 502]
    assert all(response.json() == {
        "detail": {
            "code": "upstream_invalid",
            "message": "WhoSampled returned an unexpected response.",
        }
    } for response in responses)
    assert fetches == 2
