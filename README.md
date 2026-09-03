# WhoSampled Samples API

This project provides a synchronous, local-only FastAPI endpoint for Sample Uses attributed to
an exact WhoSampled artist slug. A visible Camoufox browser acquires Cloudflare clearance, then
the API reuses that coupled session for serialized browserless requests through `curl_cffi`.

## Clean setup on Windows

Install Python 3.13, then run these commands in PowerShell from the repository root:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
python -m camoufox set official/stable/135.0.1-beta.24
python -m camoufox fetch
```

The dependencies in `pyproject.toml` and the Camoufox browser command above are pinned to the
versions used by this project. The browser pin must remain aligned with the `firefox135`
fingerprint used by `curl_cffi`; a different browser major version fails clearance acquisition
before a browserless request.

## Run locally

Start Uvicorn on the loopback interface only:

```powershell
python -m uvicorn wsmpld.api:app --host 127.0.0.1 --port 8000 --workers 1 --no-access-log
```

The API deliberately has no authentication or CORS configuration because it is restricted to
the local machine. Keep one worker. Clearance, caching, request serialization, and request pacing
all live in one process. The `--no-access-log` option keeps opaque cursor values out of Uvicorn's
request logs.

The first accepted lookup opens visible Camoufox and may spend up to 90 seconds acquiring
Cloudflare clearance. Leave that browser open. The API then reuses its cookies and user agent for
browserless requests through `curl_cffi`.

## What the endpoint returns

`GET /artists/{artist_slug}/samples` returns Sample Uses attributed to one artist. A Sample Use is
the relationship between a Sampling Recording and a Source Recording. A Sampling Recording can
use several sources, so the API returns one Sample Use for each Source Recording. Seeing the same
Sampling Recording in consecutive items is normal.

For example, Kanye West's `Bound 2` has several Source Recordings. With `limit=1`, consecutive
responses can contain `Bound`, `Sweet Nothin's`, and `Aeroplane (Reprise)`. The Sampling Recording
stays `Bound 2`; the Source Recording changes.

The response has three parts:

`GET /sample-uses/{sample_use_id}` retrieves one direct Sample Use by its positive numeric
WhoSampled ID. The response identifies the Sampling Recording and Source Material separately.
Deleted relationships return a stable not-found response, while interpolations and other
non-direct connections return a stable unsupported-connection response.

| Field | Meaning |
|---|---|
| `artist` | The requested slug, parsed artist name, and canonical Samples URL. |
| `items` | Ordered Sample Uses from the current cursor position. |
| `pagination.returned` | Number of Sample Uses in this response. |
| `pagination.next_cursor` | Opaque position for the next request, or `null` at completion. |
| `pagination.has_more` | `true` exactly when `next_cursor` is not `null`. |

The API does not expose WhoSampled page numbers, total results, or a page count.

## Use the interactive API

Open http://127.0.0.1:8000/docs, expand `GET /artists/{artist_slug}/samples`, and select
`Try it out`.

Enter these values for the first request:

| Parameter | First request | What it does |
|---|---|---|
| `artist_slug` | `Kanye-West` | Selects the exact, case-sensitive slug from a WhoSampled artist URL. |
| `limit` | `1` | Returns at most one Sample Use from the current source page. |
| `cursor` | Leave empty | Starts a new traversal at the first item. |

A successful first response has HTTP status `200`, one item, and pagination like this:

```json
{
  "next_cursor": "<opaque cursor>",
  "returned": 1,
  "has_more": true
}
```

Copy `pagination.next_cursor` from the response. Paste it into the cursor field for the next
request and execute again. Swagger handles URL encoding. Repeat with each newly returned cursor.
Stop when `next_cursor` is `null` and `has_more` is `false`.

The cursor points to the next item, not the item just returned. Cursors for nearby items on the
same source page look similar because only their internal position changes.
Sending the same cursor again returns the same batch while the cached source page remains
unchanged. The server does not store cursor progress.

Treat cursors as opaque values. Do not decode, edit, or construct them in client code. A cursor is
bound to its artist, so a Kanye West cursor cannot continue a different artist's traversal.

## Choose a limit

A positive numeric limit returns at most that many Sample Uses from the current cursor position.
The default is `1`. `limit=max` returns everything left on the current source page.

One request does not cross a source-page boundary. If only two items remain on page 1, `limit=5`
returns those two and provides a cursor for the start of page 2. The next request fetches page 2.
Callers may change the limit between requests because the cursor does not store the batch size.

Typical requests look like this:

```text
request 1: cursor empty, limit=1
request 2: cursor from request 1, limit=3
request 3: cursor from request 2, limit=max
```

## Traverse the complete collection

This PowerShell loop starts with one item, switches to `limit=max`, and stops on the final page:

```powershell
$base = "http://127.0.0.1:8000/artists/Kanye-West/samples"
$cursor = $null
$limit = 1
$batch = 0
$allItems = @()
while ($true) {
    $query = "limit=$limit"
    if ($null -ne $cursor) {
        $query += "&cursor=" + [uri]::EscapeDataString($cursor)
    }
    $page = Invoke-RestMethod "$base`?$query"
    $batch++
    $allItems += @($page.items)
    Write-Host (
        "batch={0} returned={1} has_more={2} next_cursor={3}" -f
        $batch,
        $page.pagination.returned,
        $page.pagination.has_more,
        ($null -ne $page.pagination.next_cursor)
    )
    $cursor = $page.pagination.next_cursor
    if ($null -eq $cursor) { break }
    $limit = "max"
}
Write-Host "items=$($allItems.Count)"
Write-Host "traversal complete=$($null -eq $cursor)"
```

For a multi-page collection, successful output follows this shape. Counts depend on current
WhoSampled data.

```text
batch=1 returned=1 has_more=True next_cursor=True
batch=2 returned=9 has_more=True next_cursor=True
batch=3 returned=7 has_more=False next_cursor=False
items=17
traversal complete=True
```

Each API request fetches at most one upstream source page. A cursor request within a cached page
does not contact WhoSampled again. Completion does not trigger an extra empty-page request.

## Handle errors

Errors use a stable `detail.code` and `detail.message` response body.

| HTTP status and code | Meaning | Client action |
|---|---|---|
| `400 invalid_cursor` | The cursor is malformed, unsupported, impossible, or bound to another artist. | Restart without a cursor. |
| `404 artist_not_found` | WhoSampled did not find the exact artist slug. | Check the slug and its capitalization. |
| `404 sample_use_not_found` | WhoSampled no longer has the requested Sample Use. | Record the missing observation and do not retry unchanged input. |
| `422 unsupported_connection_type` | The relationship is not a direct Sample Use. | Exclude it from direct-audio collection. |
| `409 collection_changed` | Live data invalidated the saved item position. | Discard the cursor and restart the traversal. |
| `502 upstream_invalid` | WhoSampled returned invalid markup, status, or redirect data. | Stop and inspect the server logs. |
| `503 clearance_failed` | Camoufox could not acquire or refresh a reusable session. | Retry after clearance can run successfully. |
| `503 upstream_rate_limited` | WhoSampled returned HTTP 429. | Wait for `Retry-After` when the response includes it. |
| `504 lookup_timeout` | The complete lookup exceeded 120 seconds. | Retry after checking clearance and upstream access. |

The API does not retry an upstream rate limit. It forwards `Retry-After` only when WhoSampled
provides a valid delay or HTTP date.

## Live data, caching, and pacing

The cursor traverses a live collection and does not create a snapshot or expire with time. Treat
cursors as opaque values and do not construct or edit them. Changes that keep a saved position
valid cannot be detected, so live reorderings may cause duplicates or omissions across requests.

The parsed-page cache keeps successful pages for 10 minutes and stores at most 256 pages. Requests
for the same uncached page share one in-progress fetch. The cache does not retain failures or
challenge responses.

Each uncached WhoSampled data request starts at least four seconds after the previous one, plus
random jitter from zero to one second. A clearance retry follows the same rule, and pacing wait
time counts against the 120-second lookup deadline. Cache hits do not wait because they do not
contact WhoSampled.

## Verify

Run the deterministic suite and static checks:

```powershell
python -m pytest
python -m ruff check .
python -m mypy
```

The live tests open visible Camoufox, start Uvicorn, and contact WhoSampled. Run the focused
multi-page traversal after changing cursor behavior, page parsing, caching, pacing, or logs:

```powershell
python -m pytest -m live tests/test_live_samples.py::test_live_small_collection_traverses_with_cursors_until_completion -q -s
```

Run all live checks before treating the complete API as verified:

```powershell
python -m pytest -m live tests/test_live_samples.py -q -s
```

The focused command should finish with `1 passed`; the full command should finish with `3 passed`.
A clearance timeout, Cloudflare challenge, non-200 response, or assertion failure needs
investigation.

## Manual diagnostics

The low-level diagnostics remain available independently of the API:

```powershell
python -m diagnostics.camoufox_clearance Structure
python -m diagnostics.cookie_reuse
```

Both commands open visible Camoufox. They exit with status 0 only after proving the intended
behavior, and exit nonzero when clearance or browserless cookie reuse fails. The Camoufox
diagnostic saves `whosampled_test.png` for visual inspection.
