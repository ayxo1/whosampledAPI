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
python -m uvicorn wsmpld.api:app --host 127.0.0.1 --port 8000
```

The API deliberately has no authentication or CORS configuration because it is restricted to
the local machine. Open http://127.0.0.1:8000/docs for the generated interactive documentation,
or request a Sample Use directly:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/artists/Kanye-West/samples
Invoke-RestMethod "http://127.0.0.1:8000/artists/Kanye-West/samples?limit=max"
```

The first accepted lookup can open visible Camoufox for up to 90 seconds. A complete lookup has
a 120-second deadline. Later requests reuse unexpired clearance, and all upstream WhoSampled
operations are serialized within the process.

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
