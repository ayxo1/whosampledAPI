from pathlib import Path

import pytest

from wsmpld.parser import parse_samples_page

FIXTURES = Path(__file__).parent / "fixtures"


def test_recording_years_and_explicit_producer_credit_are_nullable() -> None:
    document = (FIXTURES / "nullable_recording_fields.html").read_text(encoding="utf-8")

    parsed = parse_samples_page(document)

    assert parsed.artist_name == "Unknown Artist"
    assert len(parsed.items) == 1
    assert parsed.items[0].sampling_recording.year is None
    assert parsed.items[0].sampling_recording.producer_credit is None
    assert parsed.items[0].source_recording.year is None


def test_external_recording_url_is_rejected() -> None:
    document = (FIXTURES / "one_sample_use.html").read_text(encoding="utf-8")
    document = document.replace(
        'href="/Kanye-West/Power/"',
        'href="//evil.example/Kanye-West/Power/"',
    )

    with pytest.raises(ValueError, match="WhoSampled URL"):
        parse_samples_page(document)


def test_display_groups_are_flattened_in_order_without_deduplication() -> None:
    document = (FIXTURES / "grouped_sample_uses.html").read_text(encoding="utf-8")

    parsed = parse_samples_page(document)

    assert parsed.artist_name == "Example Artist"
    assert [item.source_recording.title for item in parsed.items] == [
        "First Source",
        "Second Source",
        "Second Source",
    ]
    assert len(parsed.items) == 3
    assert parsed.items[0].sampling_recording.model_dump(mode="json") == {
        "title": "Sampling Recording",
        "artist_credit": "Example Artist feat. Guest",
        "year": 2024,
        "producer_credit": "Example Producer",
        "url": "https://www.whosampled.com/Example-Artist/Sampling-Recording/",
    }
    assert parsed.items[0].source_recording.model_dump(mode="json") == {
        "title": "First Source",
        "artist_credit": "Source Artist",
        "year": 1971,
        "url": "https://www.whosampled.com/Source-Artist/First-Source/",
    }
    assert parsed.items[1] == parsed.items[2]


def test_explicit_empty_samples_collection_is_valid() -> None:
    document = (FIXTURES / "empty_sample_uses.html").read_text(encoding="utf-8")

    parsed = parse_samples_page(document)

    assert parsed.artist_name == "No Samples Artist"
    assert parsed.items == []


def test_unrecognized_samples_markup_fails_closed() -> None:
    document = (
        "<html><body><main data-artist-name='Changed Site'>"
        "<div class='unknown-result'>A relationship that cannot be parsed</div>"
        "</main></body></html>"
    )

    with pytest.raises(ValueError, match="Unrecognized Samples collection"):
        parse_samples_page(document)


def test_malformed_relationship_prevents_partial_parser_results() -> None:
    document = (FIXTURES / "multiple_sample_uses.html").read_text(encoding="utf-8")
    document = document.replace(
        '<a class="title" href="/Kanye-West/Power/">Power</a>',
        "",
    )

    with pytest.raises(ValueError, match="Missing required"):
        parse_samples_page(document)
