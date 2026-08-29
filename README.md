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
python -m camoufox fetch
```

The dependencies in `pyproject.toml` are pinned to the versions used by this project.

## Run locally

Start Uvicorn on the loopback interface only:

```powershell
python -m uvicorn wsmpld.api:app --host 127.0.0.1 --port 8000 --workers 1
```

The API deliberately has no authentication or CORS configuration because it is restricted to
the local machine. Open http://127.0.0.1:8000/docs for the generated interactive documentation,
or start a Samples traversal directly:

```powershell
$page = Invoke-RestMethod "http://127.0.0.1:8000/artists/Kanye-West/samples?limit=5"
$page.items
$cursor = [uri]::EscapeDataString($page.pagination.next_cursor)
$nextPage = Invoke-RestMethod "http://127.0.0.1:8000/artists/Kanye-West/samples?cursor=$cursor&limit=max"
```

Omit `cursor` for the first request. A numeric limit can stop within the current WhoSampled page,
while `limit=max` returns the rest of that page. Continue with `pagination.next_cursor` until it is
null. Each request fetches at most one upstream page, and clients may change the limit between
requests.

The cursor traverses a live collection and does not create a snapshot or expire with time. If
WhoSampled changes a page so a saved position is no longer valid, the API returns
`409 collection_changed`; restart without a cursor. Treat cursors as opaque values and do not
construct or edit them. Changes that keep a saved position valid cannot be detected, so live
reorderings may cause duplicates or omissions across requests.

The first accepted lookup can open visible Camoufox for up to 90 seconds. A complete lookup has
a 120-second deadline. Later requests reuse unexpired clearance, and all upstream WhoSampled
operations are serialized within the process. Run exactly one worker because clearance and
request pacing are process-local.

Each uncached WhoSampled data request starts at least four seconds after the previous one, plus
random jitter from zero to one second. A clearance retry follows the same rule, and pacing wait
time counts against the 120-second lookup deadline. Cache hits do not wait because they do not
contact WhoSampled.

If WhoSampled returns HTTP 429, the API does not refresh clearance or retry. It returns
`503 upstream_rate_limited` instead. When WhoSampled supplies a valid `Retry-After` delay or HTTP
date, the API forwards that header so the client can decide when to try again. It drops malformed
values.

## Verify

Run the deterministic suite and static checks:

```powershell
python -m pytest
python -m ruff check .
python -m mypy
```

The live test opens visible Camoufox and contacts WhoSampled. Run it before treating the complete
slice as verified:

```powershell
python -m pytest -m live tests/test_live_samples.py -q
```

A Cloudflare challenge or failure in the live test is a real failure and must be investigated.

## Manual diagnostics

The low-level diagnostics remain available independently of the API:

```powershell
python -m diagnostics.camoufox_clearance Structure
python -m diagnostics.cookie_reuse
```

Both commands open visible Camoufox. They exit with status 0 only after proving the intended
behavior, and exit nonzero when clearance or browserless cookie reuse fails. The Camoufox
diagnostic saves `whosampled_test.png` for visual inspection.
