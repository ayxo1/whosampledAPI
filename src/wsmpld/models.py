from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, HttpUrl


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
    url: WhoSampledUrl


class SampleUse(BaseModel):
    sampling_recording: SamplingRecording
    source_recording: SourceRecording


class Artist(BaseModel):
    requested_slug: str
    name: str
    samples_url: WhoSampledUrl


class Pagination(BaseModel):
    source_page: int
    returned: int
    has_more: bool


class SamplesResponse(BaseModel):
    artist: Artist
    items: list[SampleUse]
    pagination: Pagination


class ErrorDetail(BaseModel):
    code: Literal["artist_not_found", "upstream_invalid"]
    message: str


class ErrorResponse(BaseModel):
    detail: ErrorDetail
