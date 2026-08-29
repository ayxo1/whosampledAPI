import base64
import binascii
import json
from dataclasses import dataclass
from unicodedata import normalize


class InvalidCursorError(ValueError):
    """A public Samples cursor could not have been issued by this API."""


@dataclass(frozen=True)
class CursorPosition:
    artist_slug: str
    page_number: int
    item_offset: int

    def __post_init__(self) -> None:
        if not self.artist_slug or len(self.artist_slug) > 200:
            raise InvalidCursorError("Invalid cursor artist")
        if type(self.page_number) is not int or self.page_number < 1:
            raise InvalidCursorError("Invalid cursor page")
        if type(self.item_offset) is not int or self.item_offset < 0:
            raise InvalidCursorError("Invalid cursor offset")
        if self.page_number == 1 and self.item_offset == 0:
            raise InvalidCursorError("Impossible initial cursor position")


def encode_cursor(position: CursorPosition) -> str:
    payload = json.dumps(
        {
            "artist": normalize("NFC", position.artist_slug),
            "offset": position.item_offset,
            "page": position.page_number,
            "version": 1,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")


def decode_cursor(value: str) -> CursorPosition:
    if not value or len(value) > 2_048 or "=" in value:
        raise InvalidCursorError("Invalid cursor encoding")
    padding = "=" * (-len(value) % 4)
    try:
        decoded = base64.b64decode(value + padding, altchars=b"-_", validate=True)
        payload: object = json.loads(decoded.decode("utf-8"))
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise InvalidCursorError("Invalid cursor encoding") from error
    if not isinstance(payload, dict) or set(payload) != {
        "artist",
        "offset",
        "page",
        "version",
    }:
        raise InvalidCursorError("Invalid cursor payload")
    if type(payload["version"]) is not int or payload["version"] != 1:
        raise InvalidCursorError("Unsupported cursor version")
    artist_slug = payload["artist"]
    if not isinstance(artist_slug, str) or artist_slug != normalize("NFC", artist_slug):
        raise InvalidCursorError("Invalid cursor artist")
    return CursorPosition(
        artist_slug=artist_slug,
        page_number=payload["page"],
        item_offset=payload["offset"],
    )
