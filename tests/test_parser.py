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
