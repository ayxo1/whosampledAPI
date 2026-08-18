from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from contextlib import contextmanager
from pathlib import Path
from threading import Event, Lock
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from wsmpld.api import app, get_samples_page
from wsmpld.upstream import (
    ArtistNotFoundError,
    BrowserlessResponse,
    BrowserlessSamplesPage,
    ClearanceFailedError,
    ClearanceSession,
    FetchSamplesPage,
    LookupTimeoutError,
    SamplesPage,
)

FIXTURES = Path(__file__).parent / "fixtures"


@contextmanager
def _override_samples_page(fetch_samples_page: FetchSamplesPage) -> Iterator[None]:
    app.dependency_overrides[get_samples_page] = lambda: fetch_samples_page
    try:
        yield
    finally:
        app.dependency_overrides.clear()


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
        "pagination": {"source_page": 1, "returned": 1, "has_more": False},
    }


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
    received_slugs: list[str] = []
    page = SamplesPage(
        html=(FIXTURES / "one_sample_use.html").read_text(encoding="utf-8"),
        resolved_url="https://www.whosampled.com/Bjork/samples/",
    )

    def fetch(artist_slug: str) -> SamplesPage:
        received_slugs.append(artist_slug)
        return page

    encoded_slug = quote(requested_slug, safe="")

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
    ]
    assert operation["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/SamplesResponse"
    }
    assert operation["responses"]["404"]["content"]["application/json"]["schema"] == {
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
        ],
        "title": "Code",
    }
    assert {
        status: operation["responses"][status]["content"]["application/json"]["example"]
        for status in ("404", "502", "503", "504")
    } == {
        "404": {
            "detail": {"code": "artist_not_found", "message": "Artist was not found."}
        },
        "502": {
            "detail": {
                "code": "upstream_invalid",
                "message": "WhoSampled returned an unexpected response.",
            }
        },
        "503": {
            "detail": {
                "code": "clearance_failed",
                "message": "Could not acquire a reusable upstream session.",
            }
        },
        "504": {
            "detail": {
                "code": "lookup_timeout",
                "message": "The lookup exceeded its 120-second time limit.",
            }
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
        "source_page": 1,
        "returned": 2,
        "has_more": False,
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
    assert limited_response.json()["pagination"] == {
        "source_page": 1,
        "returned": 1,
        "has_more": True,
    }
    assert oversized_response.status_code == 200
    assert len(oversized_response.json()["items"]) == 2
    assert oversized_response.json()["pagination"] == {
        "source_page": 1,
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
    assert response.json()["pagination"] == {
        "source_page": 1,
        "returned": 2,
        "has_more": True,
    }


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


def test_complete_lookup_timeout_has_stable_gateway_timeout_response() -> None:
    def timed_out_lookup(artist_slug: str) -> SamplesPage:
        raise LookupTimeoutError(artist_slug)

    with _override_samples_page(timed_out_lookup):
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


def test_challenged_browserless_fetch_refreshes_clearance_once_and_retries() -> None:
    acquisitions = 0
    fetches = 0
    page_html = (FIXTURES / "one_sample_use.html").read_text(encoding="utf-8")

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
        if fetches == 1:
            return BrowserlessResponse(
                status_code=200,
                text="<title>Just a moment...</title>",
                resolved_url=url,
            )
        return BrowserlessResponse(status_code=200, text=page_html, resolved_url=url)

    fetch_samples_page = BrowserlessSamplesPage(
        acquire_clearance=acquire_clearance,
        fetch_browserlessly=fetch_browserlessly,
        monotonic=lambda: 0.0,
    )

    with _override_samples_page(fetch_samples_page):
        response = TestClient(app).get("/artists/Kanye-West/samples")

    assert response.status_code == 200
    assert acquisitions == 2
    assert fetches == 2


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
    )

    with _override_samples_page(fetch_samples_page):
        response = TestClient(app).get("/artists/Kanye-West/samples")

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "clearance_failed"
    assert acquisitions == 2
    assert fetches == 2


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

    fetch_samples_page = BrowserlessSamplesPage(
        acquire_clearance=acquire_clearance,
        fetch_browserlessly=fetch_browserlessly,
        monotonic=lambda: 0.0,
    )

    def request_samples() -> int:
        return TestClient(app).get("/artists/Kanye-West/samples").status_code

    with _override_samples_page(fetch_samples_page), ThreadPoolExecutor(max_workers=2) as pool:
        first_response = pool.submit(request_samples)
        assert first_fetch_started.wait(timeout=2)
        second_response = pool.submit(request_samples)
        try:
            with pytest.raises(FutureTimeoutError):
                second_response.result(timeout=0.5)
        finally:
            release_first_fetch.set()

        assert first_response.result(timeout=2) == 200
        assert second_response.result(timeout=2) == 200

    assert acquisitions == 1
    assert fetches == 2


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
        "pagination": {"source_page": 1, "returned": 0, "has_more": False},
    }


def test_invalid_resolved_upstream_url_has_stable_bad_gateway_response() -> None:
    page = SamplesPage(
        html=(FIXTURES / "one_sample_use.html").read_text(encoding="utf-8"),
        resolved_url="https://evil.example/Kanye-West/samples/",
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
