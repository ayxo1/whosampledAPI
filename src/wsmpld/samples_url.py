from urllib.parse import parse_qsl, quote, urljoin, urlsplit

BASE_URL = "https://www.whosampled.com"


def resolved_samples_page_number(url: str, artist_slug: str) -> int:
    try:
        parsed = urlsplit(url)
        port = parsed.port
        query = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True)
    except ValueError as error:
        raise ValueError("Malformed Samples page URL") from error

    if (
        parsed.scheme != "https"
        or parsed.hostname != "www.whosampled.com"
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or parsed.path != f"/{quote(artist_slug, safe='')}/samples/"
        or parsed.fragment
    ):
        raise ValueError("Unsafe Samples page URL")

    if not query:
        return 1
    if len(query) != 1 or query[0][0] != "sp":
        raise ValueError("Malformed Samples page query")
    page_value = query[0][1]
    if (
        not page_value.isascii()
        or not page_value.isdecimal()
        or int(page_value) < 2
        or str(int(page_value)) != page_value
    ):
        raise ValueError("Malformed Samples page number")
    return int(page_value)


def samples_page_link_number(href: str, artist_slug: str) -> int:
    return resolved_samples_page_number(urljoin(BASE_URL, href), artist_slug)
