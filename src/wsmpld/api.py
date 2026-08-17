from typing import Annotated

from fastapi import Depends, FastAPI, Path, Query
from pydantic import AfterValidator, HttpUrl

from wsmpld.models import Artist, Pagination, SamplesResponse
from wsmpld.parser import parse_samples_page
from wsmpld.upstream import FetchSamplesPage, unavailable_samples_page


def _validate_artist_slug(value: str) -> str:
    if (
        not value.strip()
        or value.strip(".") == ""
        or "/" in value
        or "\\" in value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError("artist slug contains an unsafe path value")
    return value


ArtistSlug = Annotated[
    str,
    Path(
        min_length=1,
        max_length=200,
        title="Exact WhoSampled artist slug",
        description="One exact, case-sensitive artist slug from a WhoSampled URL.",
    ),
    AfterValidator(_validate_artist_slug),
]


def get_samples_page() -> FetchSamplesPage:
    return unavailable_samples_page


app = FastAPI(
    title="WhoSampled Samples API",
    description="A local API for Sample Uses attributed to a requested artist.",
)


@app.get(
    "/artists/{artist_slug:path}/samples",
    response_model=SamplesResponse,
    summary="Get an artist's Samples",
)
def read_samples(
    artist_slug: ArtistSlug,
    fetch_samples_page: Annotated[FetchSamplesPage, Depends(get_samples_page)],
    limit: Annotated[
        int,
        Query(gt=0, description="Maximum Sample Uses to return from source page 1."),
    ] = 1,
) -> SamplesResponse:
    page = fetch_samples_page(artist_slug)
    parsed = parse_samples_page(page.html)
    items = parsed.items[:limit]
    return SamplesResponse(
        artist=Artist(
            requested_slug=artist_slug,
            name=parsed.artist_name,
            samples_url=HttpUrl(page.resolved_url),
        ),
        items=items,
        pagination=Pagination(
            source_page=1,
            returned=len(items),
            has_more=len(parsed.items) > len(items),
        ),
    )
