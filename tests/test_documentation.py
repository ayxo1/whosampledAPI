from pathlib import Path


def test_readme_documents_the_complete_local_workflow() -> None:
    readme = (Path(__file__).parents[1] / "README.md").read_text(encoding="utf-8")

    assert "Python 3.13" in readme
    assert "python -m venv .venv" in readme
    assert 'python -m pip install -e ".[dev]"' in readme
    assert (
        "python -m camoufox set official/stable/135.0.1-beta.24"
        in readme
    )
    assert "python -m camoufox fetch" in readme
    assert (
        "python -m uvicorn wsmpld.api:app --host 127.0.0.1 --port 8000 --workers 1 "
        "--no-access-log"
        in readme
    )
    assert "http://127.0.0.1:8000/docs" in readme
    assert "GET /artists/{artist_slug}/samples" in readme
    assert "GET /sample-uses/{sample_use_id}" in readme
    assert "Try it out" in readme
    assert "exact, case-sensitive" in readme
    assert "one Sample Use for each Source Recording" in readme
    assert "The cursor points to the next item" in readme
    assert "Sending the same cursor again" in readme
    assert "does not cross a source-page boundary" in readme
    assert "next_cursor` is `null" in readme
    assert "returned=1 has_more=True next_cursor=True" in readme
    assert "traversal complete=True" in readme
    assert "pagination.next_cursor" in readme
    assert "collection_changed" in readme
    assert "live collection" in readme
    assert "duplicates or omissions" in readme
    assert "four seconds" in readme
    assert "zero to one second" in readme
    assert "upstream_rate_limited" in readme
    assert "Retry-After" in readme
    assert "Cache hits do not wait" in readme
    assert "restart without a cursor" in readme.lower()
    assert "restart the traversal" in readme
    assert "400 invalid_cursor" in readme
    assert "503 upstream_rate_limited" in readme
    assert "404 artist_not_found" in readme
    assert "404 sample_use_not_found" in readme
    assert "422 unsupported_connection_type" in readme
    assert "502 upstream_invalid" in readme
    assert "504 lookup_timeout" in readme
    assert "while ($true)" in readme
    assert "$null -ne $cursor" in readme
    assert '$page = Invoke-RestMethod "$base`?$query"' in readme
    assert "$cursor = $page.pagination.next_cursor" in readme
    assert "if ($null -eq $cursor) { break }" in readme
    assert '$limit = "max"' in readme
    assert "python -m pytest" in readme
    assert 'python -m pytest -m live tests/test_live_samples.py -q' in readme
    assert (
        "python -m pytest -m live "
        "tests/test_live_samples.py::"
        "test_live_small_collection_traverses_with_cursors_until_completion "
        "-q -s"
        in readme
    )
    assert "python -m ruff check ." in readme
    assert "python -m mypy" in readme
    assert "python -m diagnostics.camoufox_clearance" in readme
    assert "python -m diagnostics.cookie_reuse" in readme
