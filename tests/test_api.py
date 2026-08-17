from pathlib import Path
from urllib.parse import quote

from fastapi.testclient import TestClient

from wsmpld.api import app, get_samples_page
from wsmpld.upstream import SamplesPage

FIXTURES = Path(__file__).parent / "fixtures"


def test_user_receives_one_sample_use_by_default() -> None:
    page = SamplesPage(
        html=(FIXTURES / "one_sample_use.html").read_text(encoding="utf-8"),
        resolved_url="https://www.whosampled.com/Kanye-West/samples/",
    )
    app.dependency_overrides[get_samples_page] = lambda: lambda artist_slug: page

    try:
        response = TestClient(app).get("/artists/Kanye-West/samples")
    finally:
        app.dependency_overrides.clear()

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

    app.dependency_overrides[get_samples_page] = lambda: fetch
    encoded_slug = quote(requested_slug, safe="")

    try:
        response = TestClient(app).get(f"/artists/{encoded_slug}/samples")
    finally:
        app.dependency_overrides.clear()

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
