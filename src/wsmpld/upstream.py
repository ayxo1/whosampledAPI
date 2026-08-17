from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class SamplesPage:
    html: str
    resolved_url: str


class FetchSamplesPage(Protocol):
    def __call__(self, artist_slug: str) -> SamplesPage: ...


def unavailable_samples_page(artist_slug: str) -> SamplesPage:
    del artist_slug
    raise RuntimeError("Live WhoSampled retrieval is not implemented yet")
