from typing import Annotated, Literal

from fastapi import Depends, FastAPI, HTTPException, Path, Query
from pydantic import AfterValidator, Field, HttpUrl, ValidationError

from wsmpld.models import Artist, ErrorResponse, Pagination, SamplesResponse
from wsmpld.parser import parse_samples_page
from wsmpld.upstream import (
    ArtistNotFoundError,
    FetchSamplesPage,
    unavailable_samples_page,
)


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


def _upstream_invalid() -> HTTPException:
    return HTTPException(
        status_code=502,
        detail={
            "code": "upstream_invalid",
            "message": "WhoSampled returned an unexpected response.",
        },
    )


app = FastAPI(
    title="WhoSampled Samples API",
    description="A local API for Sample Uses attributed to a requested artist.",
)


@app.get(
    "/artists/{artist_slug:path}/samples",
    response_model=SamplesResponse,
    responses={
        404: {"model": ErrorResponse, "description": "Artist was not found."},
        502: {"model": ErrorResponse, "description": "WhoSampled response was invalid."},
    },
    summary="Get an artist's Samples",
)
def read_samples(
    artist_slug: ArtistSlug,
    fetch_samples_page: Annotated[FetchSamplesPage, Depends(get_samples_page)],
    limit: Annotated[
        Annotated[int, Field(gt=0)] | Literal["max"],
        Query(description="Maximum Sample Uses to return from source page 1, or 'max'."),
    ] = 1,
) -> SamplesResponse:
    try:
        page = fetch_samples_page(artist_slug)
    except ArtistNotFoundError as error:
        raise HTTPException(
            status_code=404,
            detail={"code": "artist_not_found", "message": "Artist was not found."},
        ) from error
    except Exception as error:
        raise _upstream_invalid() from error
    try:
        parsed = parse_samples_page(page.html)
    except ValueError as error:
        raise _upstream_invalid() from error
    items = parsed.items if limit == "max" else parsed.items[:limit]
    try:
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
    except ValidationError as error:
        raise _upstream_invalid() from error
