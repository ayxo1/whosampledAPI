from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi.testclient import TestClient

from wsmpld.api import app, get_samples_page
from wsmpld.upstream import (
    ArtistNotFoundError,
    FetchSamplesPage,
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
    assert schema["components"]["schemas"]["ErrorDetail"]["properties"]["code"] == {
        "type": "string",
        "enum": ["artist_not_found", "upstream_invalid"],
        "title": "Code",
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
