import re
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


def _xpath_has_class(name: str) -> str:
    return f"contains(concat(' ', normalize-space(@class), ' '), ' {name} ')"


def _live_year(element: html.HtmlElement, selector: str) -> int | None:
    value = _optional_text(element, selector)
    if value is None:
        return None
    match = re.fullmatch(r"\((\d{4})\)", value)
    if match is None:
        raise ValueError("Malformed recording year")
    return int(match.group(1))


def _live_samples_page(root: html.HtmlElement) -> ParsedSamplesPage:
    artist_elements = _elements(root, f"//*[{_xpath_has_class('artistName')}]")
    if len(artist_elements) != 1:
        raise ValueError("Missing artist metadata")
    artist_name = " ".join(artist_elements[0].text_content().split())
    if not artist_name:
        raise ValueError("Missing artist name")

    track_lists = _elements(root, f"//*[{_xpath_has_class('trackList')}]")
    if len(track_lists) != 1:
        raise ValueError("Unrecognized Samples collection")
    tracks = _elements(track_lists[0], f".//*[{_xpath_has_class('trackItem')}]")
    if not tracks:
        raise ValueError("Unrecognized Samples collection")

    items: list[SampleUse] = []
    for track in tracks:
        title_links = _elements(
            track,
            f".//h3[{_xpath_has_class('trackName')}]//a[@itemprop='url'][@href]",
        )
        if len(title_links) != 1:
            raise ValueError("Malformed Sampling Recording")
        sampling_title = _text(title_links[0], ".//*[@itemprop='name']")
        sampling_href = title_links[0].get("href")
        if sampling_href is None:
            raise ValueError("Missing required recording URL")
        credit_selector = f".//*[{_xpath_has_class('trackArtistName')}]"
        credit_elements = _elements(track, credit_selector)
        if credit_elements:
            sampling_credit = " ".join(_text(track, credit_selector).split())
            if not sampling_credit.startswith("by "):
                raise ValueError("Malformed Sampling Recording artist credit")
            sampling_credit = sampling_credit.removeprefix("by ")
        else:
            sampling_credit = artist_name
        sampling_year = _live_year(track, f".//*[{_xpath_has_class('trackYear')}]")
        producer = _optional_text(track, ".//*[contains(@class, 'producer')]")
        if producer is not None and producer.lower().startswith("produced by "):
            producer = producer[len("produced by ") :]

        sources = _elements(
            track,
            f".//*[{_xpath_has_class('track-connection')}]//li",
        )
        if not sources:
            raise ValueError("Malformed Sample Use")
        for source in sources:
            source_links = _elements(
                source, f".//a[{_xpath_has_class('connectionName')}][@href]"
            )
            if len(source_links) != 1:
                raise ValueError("Malformed Source Recording")
            source_title = " ".join(source_links[0].text_content().split())
            source_href = source_links[0].get("href")
            if not source_title or source_href is None:
                raise ValueError("Malformed Source Recording")
            source_text = " ".join(source.text_content().split())
            source_details = source_text.removeprefix(source_title).strip()
            source_match = re.fullmatch(
                r"(?:by|from)\s+(.+?)(?:\s+\((\d{4})\))?", source_details
            )
            if source_match is None:
                raise ValueError("Malformed Source Recording metadata")
            source_credit, source_year = source_match.groups()
            items.append(
                SampleUse(
                    sampling_recording=SamplingRecording(
                        title=sampling_title,
                        artist_credit=sampling_credit,
                        year=sampling_year,
                        producer_credit=producer,
                        url=HttpUrl(urljoin(BASE_URL, sampling_href)),
                    ),
                    source_recording=SourceRecording(
                        title=source_title,
                        artist_credit=source_credit,
                        year=int(source_year) if source_year is not None else None,
                        url=HttpUrl(urljoin(BASE_URL, source_href)),
                    ),
                )
            )
    return ParsedSamplesPage(artist_name=artist_name, items=items)


def parse_samples_page(document: str) -> ParsedSamplesPage:
    root = html.fromstring(document)
    main = _elements(root, "//main[@data-artist-name]")
    if not main:
        return _live_samples_page(root)

    artist_name = main[0].get("data-artist-name")
    if not artist_name:
        raise ValueError("Missing artist name")

    relationships = _elements(
        main[0],
        ".//article[contains(concat(' ', normalize-space(@class), ' '), ' sample-use ')]",
    )
    empty_markers = _elements(
        main[0],
        ".//*[contains(concat(' ', normalize-space(@class), ' '), ' no-sample-uses ')]",
    )
    if not relationships and len(empty_markers) != 1:
        raise ValueError("Unrecognized Samples collection")
    if relationships and empty_markers:
        raise ValueError("Unrecognized Samples collection")

    items: list[SampleUse] = []
    for relationship in relationships:
        sampling = _elements(
            relationship,
            ".//section[contains(concat(' ', normalize-space(@class), ' '), "
            "' sampling-recording ')]",
        )
        source = _elements(
            relationship,
            ".//section[contains(concat(' ', normalize-space(@class), ' '), ' source-recording ')]",
        )
        if len(sampling) != 1 or not source:
            raise ValueError("Malformed Sample Use")

        sampling_recording = _recording(sampling[0])
        producer = _optional_text(
            sampling[0],
            ".//*[contains(concat(' ', normalize-space(@class), ' '), ' producer-credit ')]",
        )
        if producer is not None and producer.lower().startswith("produced by "):
            producer = producer[len("produced by ") :]

        for source_element in source:
            source_recording = _recording(source_element)
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
