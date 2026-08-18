import logging
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, HTTPException, Path, Query
from pydantic import AfterValidator, Field, HttpUrl, ValidationError

from wsmpld.models import Artist, ErrorResponse, Pagination, SamplesResponse
from wsmpld.parser import parse_samples_page
from wsmpld.upstream import (
    ArtistNotFoundError,
    ClearanceFailedError,
    FetchSamplesPage,
    LookupTimeoutError,
    live_samples_page,
)

logger = logging.getLogger("uvicorn.error")

ARTIST_NOT_FOUND_DETAIL = {
    "code": "artist_not_found",
    "message": "Artist was not found.",
}
UPSTREAM_INVALID_DETAIL = {
    "code": "upstream_invalid",
    "message": "WhoSampled returned an unexpected response.",
}
CLEARANCE_FAILED_DETAIL = {
    "code": "clearance_failed",
    "message": "Could not acquire a reusable upstream session.",
}
LOOKUP_TIMEOUT_DETAIL = {
    "code": "lookup_timeout",
    "message": "The lookup exceeded its 120-second time limit.",
}


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
    return live_samples_page


def _upstream_invalid() -> HTTPException:
    return HTTPException(status_code=502, detail=UPSTREAM_INVALID_DETAIL)


def _documented_error(description: str, detail: dict[str, str]) -> dict[str, object]:
    return {
        "model": ErrorResponse,
        "description": description,
        "content": {"application/json": {"example": {"detail": detail}}},
    }


app = FastAPI(
    title="WhoSampled Samples API",
    description="A local API for Sample Uses attributed to a requested artist.",
)


@app.get(
    "/artists/{artist_slug:path}/samples",
    response_model=SamplesResponse,
    responses={
        404: _documented_error("Artist was not found.", ARTIST_NOT_FOUND_DETAIL),
        502: _documented_error("WhoSampled response was invalid.", UPSTREAM_INVALID_DETAIL),
        503: _documented_error("Upstream clearance failed.", CLEARANCE_FAILED_DETAIL),
        504: _documented_error("Complete lookup timed out.", LOOKUP_TIMEOUT_DETAIL),
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
        raise HTTPException(status_code=404, detail=ARTIST_NOT_FOUND_DETAIL) from error
    except ClearanceFailedError as error:
        raise HTTPException(status_code=503, detail=CLEARANCE_FAILED_DETAIL) from error
    except LookupTimeoutError as error:
        raise HTTPException(status_code=504, detail=LOOKUP_TIMEOUT_DETAIL) from error
    except Exception as error:
        logger.warning("Samples fetch failed error_type=%s", type(error).__name__)
        raise _upstream_invalid() from error
    try:
        parsed = parse_samples_page(page.html)
    except ValueError as error:
        logger.warning("Samples parse failed reason=%s", error)
        raise _upstream_invalid() from error
    logger.info("parsed %d Sample Uses artist_slug=%s", len(parsed.items), artist_slug)
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
