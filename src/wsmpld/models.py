from datetime import datetime
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, Field, HttpUrl


def _require_whosampled_url(url: HttpUrl) -> HttpUrl:
    if url.scheme != "https" or url.host != "www.whosampled.com":
        raise ValueError("URL must be a secure WhoSampled URL")
    return url


WhoSampledUrl = Annotated[HttpUrl, AfterValidator(_require_whosampled_url)]


class SamplingRecording(BaseModel):
    title: str
    artist_credit: str
    year: int | None
    producer_credit: str | None
    url: WhoSampledUrl


class SourceRecording(BaseModel):
    title: str
    artist_credit: str
    year: int | None
    url: Annotated[
        WhoSampledUrl,
        Field(
            deprecated=True,
            description=(
                "Compatibility URL supplied by the artist Samples collection. "
                "Do not use it as a Source Recording identity."
            ),
        ),
    ]


class SampleUse(BaseModel):
    sample_use_id: Annotated[
        int,
        Field(
            gt=0,
            description="The positive numeric WhoSampled relationship ID.",
        ),
    ]
    sample_use_url: Annotated[
        WhoSampledUrl,
        Field(description="The canonical WhoSampled relationship URL."),
    ]
    sampling_recording: SamplingRecording
    source_recording: SourceRecording


class Artist(BaseModel):
    requested_slug: str
    name: str
    samples_url: WhoSampledUrl


class Pagination(BaseModel):
    next_cursor: str | None
    returned: int
    has_more: bool


class Observation(BaseModel):
    fetched_at: Annotated[
        datetime,
        Field(description="The time the upstream response was fetched."),
    ]
    source_url: Annotated[
        WhoSampledUrl,
        Field(description="The resolved URL of the observed upstream page."),
    ]
    content_sha256: Annotated[
        str,
        Field(
            pattern=r"^[0-9a-f]{64}$",
            description="The lowercase SHA-256 hash of the upstream response body.",
        ),
    ]
    parser_version: Annotated[
        str,
        Field(description="The Samples parser version used for this response."),
    ]


class SamplesResponse(BaseModel):
    schema_version: Annotated[
        Literal[1],
        Field(description="The public Samples response schema version."),
    ]
    artist: Artist
    items: list[SampleUse]
    pagination: Pagination
    observation: Observation


class ErrorDetail(BaseModel):
    code: Literal[
        "artist_not_found",
        "upstream_invalid",
        "clearance_failed",
        "lookup_timeout",
        "invalid_cursor",
        "collection_changed",
        "upstream_rate_limited",
    ]
    message: str


class ErrorResponse(BaseModel):
    detail: ErrorDetail
