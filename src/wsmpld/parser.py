import re
from dataclasses import dataclass
from typing import cast
from urllib.parse import urljoin, urlsplit

from lxml import html
from pydantic import HttpUrl

from wsmpld.models import (
    SampleUse,
    SamplingRecording,
    SamplingRecordingSummary,
    SourceMaterialSummary,
    SourceRecording,
)
from wsmpld.samples_url import BASE_URL, samples_page_link_number

SAMPLES_PARSER_VERSION = "1"


@dataclass(frozen=True)
class ParsedSamplesPage:
    artist_name: str
    items: list[SampleUse]
    next_page_number: int | None = None


@dataclass(frozen=True)
class _ParsedRecording:
    title: str
    artist_credit: str
    year: int | None
    url: HttpUrl


@dataclass(frozen=True)
class ParsedSampleUseDetail:
    sampling_recording: SamplingRecordingSummary
    source_material: SourceMaterialSummary


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


def _sample_use_identity(href: str) -> tuple[int, HttpUrl]:
    resolved = urljoin(BASE_URL, href)
    parsed = urlsplit(resolved)
    match = re.fullmatch(r"/sample/([1-9][0-9]*)/[^/]+/", parsed.path)
    if (
        parsed.scheme != "https"
        or parsed.netloc != "www.whosampled.com"
        or parsed.query
        or parsed.fragment
        or match is None
    ):
        raise ValueError("Invalid Sample Use URL")
    return int(match.group(1)), HttpUrl(resolved)


def _xpath_has_class(name: str) -> str:
    return f"contains(concat(' ', normalize-space(@class), ' '), ' {name} ')"


def _detail_work(root: html.HtmlElement, container_id: str) -> tuple[str, HttpUrl]:
    containers = _elements(root, f"//*[@id='{container_id}']")
    if len(containers) != 1:
        raise ValueError("Missing primary Sample Use work")
    title_links = _elements(
        containers[0],
        f".//a[{_xpath_has_class('trackName')}][@itemprop='url'][@href]",
    )
    if len(title_links) != 1:
        raise ValueError("Malformed primary Sample Use work")
    title = " ".join(title_links[0].text_content().split())
    href = title_links[0].get("href")
    if not title or href is None:
        raise ValueError("Malformed primary Sample Use work")
    return title, HttpUrl(urljoin(BASE_URL, href))


def parse_sample_use_detail(document: str) -> ParsedSampleUseDetail:
    root = html.fromstring(document)
    headings = _elements(
        root,
        f"//h2[{_xpath_has_class('section-header-title')}][starts-with(normalize-space(), "
        "'Direct Sample of ')]",
    )
    if len(headings) != 1:
        raise ValueError("Sample Use is not a direct sample")
    sampling_title, sampling_url = _detail_work(root, "sampleWrap_dest")
    source_title, source_url = _detail_work(root, "sampleWrap_source")
    return ParsedSampleUseDetail(
        sampling_recording=SamplingRecordingSummary(
            title=sampling_title,
            url=sampling_url,
        ),
        source_material=SourceMaterialSummary(
            title=source_title,
            url=source_url,
        ),
    )


def _live_year(element: html.HtmlElement, selector: str) -> int | None:
    value = _optional_text(element, selector)
    if value is None:
        return None
    match = re.fullmatch(r"\((\d{4})\)", value)
    if match is None:
        raise ValueError("Malformed recording year")
    return int(match.group(1))


def _next_page_number(
    root: html.HtmlElement,
    *,
    artist_slug: str | None,
    page_number: int,
) -> int | None:
    pagination = _elements(root, f"//*[{_xpath_has_class('pagination')}]")
    if not pagination:
        return None
    if len(pagination) != 1 or artist_slug is None:
        raise ValueError("Invalid Samples pagination controls")
    current = _elements(pagination[0], f".//*[{_xpath_has_class('curr')}]")
    if len(current) != 1 or " ".join(current[0].text_content().split()) != str(page_number):
        raise ValueError("Invalid Samples pagination controls")
    links = _elements(pagination[0], ".//a")
    destination_pages: dict[html.HtmlElement, int] = {}
    for link in links:
        href = link.get("href")
        if href is None:
            raise ValueError("Invalid Samples pagination controls")
        try:
            destination_pages[link] = samples_page_link_number(href, artist_slug)
        except ValueError as error:
            raise ValueError("Invalid Samples pagination controls") from error
    next_controls = _elements(
        pagination[0],
        f".//*[{_xpath_has_class('next')}]",
    )
    next_links = _elements(pagination[0], ".//a[@rel='next']")
    if not next_controls and not next_links:
        return None
    if len(next_controls) != 1 or len(next_links) != 1:
        raise ValueError("Invalid Samples pagination controls")
    control_links = _elements(next_controls[0], ".//a[@rel='next']")
    if control_links != next_links:
        raise ValueError("Invalid Samples pagination controls")
    next_page = destination_pages[next_links[0]]
    if next_page != page_number + 1:
        raise ValueError("Invalid Samples pagination controls")
    return next_page


def _live_samples_page(
    root: html.HtmlElement,
    *,
    artist_slug: str | None,
    page_number: int,
) -> ParsedSamplesPage:
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
            sample_use_id, sample_use_url = _sample_use_identity(source_href)
            items.append(
                SampleUse(
                    sample_use_id=sample_use_id,
                    sample_use_url=sample_use_url,
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
    return ParsedSamplesPage(
        artist_name=artist_name,
        items=items,
        next_page_number=_next_page_number(
            root,
            artist_slug=artist_slug,
            page_number=page_number,
        ),
    )


def parse_samples_page(
    document: str,
    *,
    artist_slug: str | None = None,
    page_number: int = 1,
) -> ParsedSamplesPage:
    root = html.fromstring(document)
    main = _elements(root, "//main[@data-artist-name]")
    if not main:
        return _live_samples_page(
            root,
            artist_slug=artist_slug,
            page_number=page_number,
        )

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
            relationship_href = source_element.get("data-sample-use-url")
            if relationship_href is None:
                raise ValueError("Missing required Sample Use URL")
            sample_use_id, sample_use_url = _sample_use_identity(relationship_href)
            items.append(
                SampleUse(
                    sample_use_id=sample_use_id,
                    sample_use_url=sample_use_url,
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
