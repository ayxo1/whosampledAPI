from dataclasses import dataclass
from typing import cast
from urllib.parse import urljoin

from lxml import html
from pydantic import HttpUrl

from wsmpld.models import SampleUse, SamplingRecording, SourceRecording

BASE_URL = "https://www.whosampled.com"


@dataclass(frozen=True)
class ParsedSamplesPage:
    artist_name: str
    items: list[SampleUse]


@dataclass(frozen=True)
class _ParsedRecording:
    title: str
    artist_credit: str
    year: int | None
    url: HttpUrl


def _elements(element: html.HtmlElement, expression: str) -> list[html.HtmlElement]:
    return cast(list[html.HtmlElement], element.xpath(expression))


def _text(element: html.HtmlElement, selector: str) -> str:
    matches = _elements(element, selector)
    if not matches:
        raise ValueError(f"Missing required element: {selector}")
    return cast(str, matches[0].text_content()).strip()


def _optional_text(element: html.HtmlElement, selector: str) -> str | None:
    matches = _elements(element, selector)
    if not matches:
        return None
    value = cast(str, matches[0].text_content()).strip()
    return value or None


def _year(element: html.HtmlElement) -> int | None:
    value = _optional_text(
        element,
        ".//*[contains(concat(' ', normalize-space(@class), ' '), ' year ')]",
    )
    return int(value) if value is not None else None


def _url(element: html.HtmlElement) -> HttpUrl:
    matches = _elements(
        element, ".//a[contains(concat(' ', normalize-space(@class), ' '), ' title ')][@href]"
    )
    if not matches:
        raise ValueError("Missing required recording URL")
    href = matches[0].get("href")
    if href is None:
        raise ValueError("Missing required recording URL")
    return HttpUrl(urljoin(BASE_URL, href))


def _recording(element: html.HtmlElement) -> _ParsedRecording:
    return _ParsedRecording(
        title=_text(
            element,
            ".//*[contains(concat(' ', normalize-space(@class), ' '), ' title ')]",
        ),
        artist_credit=_text(
            element,
            ".//*[contains(concat(' ', normalize-space(@class), ' '), ' artist-credit ')]",
        ),
        year=_year(element),
        url=_url(element),
    )


def parse_samples_page(document: str) -> ParsedSamplesPage:
    root = html.fromstring(document)
    main = _elements(root, "//main[@data-artist-name]")
    if not main:
        raise ValueError("Missing artist metadata")

    artist_name = main[0].get("data-artist-name")
    if not artist_name:
        raise ValueError("Missing artist name")

    items: list[SampleUse] = []
    for relationship in _elements(
        main[0],
        ".//article[contains(concat(' ', normalize-space(@class), ' '), ' sample-use ')]",
    ):
        sampling = _elements(
            relationship,
            ".//section[contains(concat(' ', normalize-space(@class), ' '), "
            "' sampling-recording ')]",
        )
        source = _elements(
            relationship,
            ".//section[contains(concat(' ', normalize-space(@class), ' '), ' source-recording ')]",
        )
        if len(sampling) != 1 or len(source) != 1:
            raise ValueError("Malformed Sample Use")

        sampling_recording = _recording(sampling[0])
        source_recording = _recording(source[0])
        producer = _optional_text(
            sampling[0],
            ".//*[contains(concat(' ', normalize-space(@class), ' '), ' producer-credit ')]",
        )
        if producer is not None and producer.lower().startswith("produced by "):
            producer = producer[len("produced by ") :]

        items.append(
            SampleUse(
                sampling_recording=SamplingRecording(
                    title=sampling_recording.title,
                    artist_credit=sampling_recording.artist_credit,
                    year=sampling_recording.year,
                    producer_credit=producer,
                    url=sampling_recording.url,
                ),
                source_recording=SourceRecording(
                    title=source_recording.title,
                    artist_credit=source_recording.artist_credit,
                    year=source_recording.year,
                    url=source_recording.url,
                ),
            )
        )

    return ParsedSamplesPage(artist_name=artist_name, items=items)
