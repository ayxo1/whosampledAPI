import logging
from typing import Annotated, Literal
from unicodedata import normalize
from urllib.parse import urlsplit, urlunsplit

from fastapi import Depends, FastAPI, HTTPException, Path, Query
from pydantic import AfterValidator, Field, HttpUrl, ValidationError

from wsmpld.cursor import CursorPosition, InvalidCursorError, decode_cursor, encode_cursor
from wsmpld.models import Artist, ErrorResponse, Pagination, SamplesResponse
from wsmpld.page_cache import ParsedSamplesPageCache
from wsmpld.upstream import (
    ArtistNotFoundError,
    ClearanceFailedError,
    FetchSamplesPage,
    LookupTimeoutError,
    SamplesPageLocation,
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
INVALID_CURSOR_DETAIL = {
    "code": "invalid_cursor",
    "message": "The Samples cursor is invalid for this request.",
}
COLLECTION_CHANGED_DETAIL = {
    "code": "collection_changed",
    "message": "The live Samples collection changed; restart the traversal.",
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


samples_page_cache = ParsedSamplesPageCache()


def get_samples_page_cache() -> ParsedSamplesPageCache:
    return samples_page_cache


def _upstream_invalid() -> HTTPException:
    return HTTPException(status_code=502, detail=UPSTREAM_INVALID_DETAIL)


def _documented_error(description: str, detail: dict[str, str]) -> dict[str, object]:
    return {
        "model": ErrorResponse,
        "description": description,
        "content": {"application/json": {"example": {"detail": detail}}},
    }


def _samples_collection_url(resolved_url: str) -> HttpUrl:
    parsed = urlsplit(resolved_url)
    return HttpUrl(urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", "")))


app = FastAPI(
    title="WhoSampled Samples API",
    description="A local API for Sample Uses attributed to a requested artist.",
)


@app.get(
    "/artists/{artist_slug:path}/samples",
    response_model=SamplesResponse,
    responses={
        400: _documented_error("Samples cursor was invalid.", INVALID_CURSOR_DETAIL),
        404: _documented_error("Artist was not found.", ARTIST_NOT_FOUND_DETAIL),
        409: _documented_error(
            "The live Samples collection changed.", COLLECTION_CHANGED_DETAIL
        ),
        502: _documented_error("WhoSampled response was invalid.", UPSTREAM_INVALID_DETAIL),
        503: _documented_error("Upstream clearance failed.", CLEARANCE_FAILED_DETAIL),
        504: _documented_error("Complete lookup timed out.", LOOKUP_TIMEOUT_DETAIL),
    },
    summary="Get an artist's Samples",
    description=(
        "Traverse the live Samples collection with an opaque continuation cursor. "
        "Each request fetches at most one upstream page. A cursor does not create a "
        "snapshot, so restart the traversal after a collection_changed response."
    ),
)
def read_samples(
    artist_slug: ArtistSlug,
    fetch_samples_page: Annotated[FetchSamplesPage, Depends(get_samples_page)],
    page_cache: Annotated[ParsedSamplesPageCache, Depends(get_samples_page_cache)],
    limit: Annotated[
        Annotated[int, Field(gt=0)] | Literal["max"],
        Query(
            description=(
                "Maximum Sample Uses to return from the current cursor position, or "
                "'max' for the remainder of the current upstream page."
            )
        ),
    ] = 1,
    cursor: Annotated[
        str | None,
        Query(
            description=(
                "Opaque continuation cursor from pagination.next_cursor. Omit it to start "
                "a new live traversal."
            )
        ),
    ] = None,
) -> SamplesResponse:
    try:
        position = decode_cursor(cursor) if cursor is not None else None
        if position is not None and position.artist_slug != normalize("NFC", artist_slug):
            raise InvalidCursorError("Cursor artist does not match request")
    except InvalidCursorError as error:
        raise HTTPException(status_code=400, detail=INVALID_CURSOR_DETAIL) from error
    page_number = position.page_number if position is not None else 1
    location = (
        SamplesPageLocation(page_number=page_number)
        if page_number > 1
        else None
    )
    try:
        cached_page = page_cache.get_or_fetch(artist_slug, location, fetch_samples_page)
    except ArtistNotFoundError as error:
        raise HTTPException(status_code=404, detail=ARTIST_NOT_FOUND_DETAIL) from error
    except ClearanceFailedError as error:
        raise HTTPException(status_code=503, detail=CLEARANCE_FAILED_DETAIL) from error
    except LookupTimeoutError as error:
        logger.info("Samples lookup timed out artist_slug=%s", artist_slug)
        raise HTTPException(status_code=504, detail=LOOKUP_TIMEOUT_DETAIL) from error
    except ValueError as error:
        logger.warning("Samples parse failed reason=%s", error)
        raise _upstream_invalid() from error
    except Exception as error:
        logger.warning("Samples fetch failed error_type=%s", type(error).__name__)
        raise _upstream_invalid() from error
    page = cached_page.source
    parsed = cached_page.parsed
    logger.info("parsed %d Sample Uses artist_slug=%s", len(parsed.items), artist_slug)
    offset = position.item_offset if position is not None else 0
    if offset > 0 and offset >= len(parsed.items):
        raise HTTPException(status_code=409, detail=COLLECTION_CHANGED_DETAIL)
    remaining_items = parsed.items[offset:]
    items = remaining_items if limit == "max" else remaining_items[:limit]
    next_offset = offset + len(items)
    next_position = None
    if next_offset < len(parsed.items):
        next_position = CursorPosition(
            artist_slug=artist_slug,
            page_number=page_number,
            item_offset=next_offset,
        )
    elif parsed.next_page_number is not None:
        next_position = CursorPosition(
            artist_slug=artist_slug,
            page_number=parsed.next_page_number,
            item_offset=0,
        )
    next_cursor = encode_cursor(next_position) if next_position is not None else None
    try:
        return SamplesResponse(
            artist=Artist(
                requested_slug=artist_slug,
                name=parsed.artist_name,
                samples_url=_samples_collection_url(page.resolved_url),
            ),
            items=items,
            pagination=Pagination(
                next_cursor=next_cursor,
                returned=len(items),
                has_more=next_cursor is not None,
            ),
        )
    except (ValidationError, ValueError) as error:
        raise _upstream_invalid() from error
