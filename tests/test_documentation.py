from pathlib import Path


def test_readme_documents_the_complete_local_workflow() -> None:
    readme = (Path(__file__).parents[1] / "README.md").read_text(encoding="utf-8")

    assert "Python 3.13" in readme
    assert "python -m venv .venv" in readme
    assert 'python -m pip install -e ".[dev]"' in readme
    assert "python -m camoufox fetch" in readme
    assert (
        "python -m uvicorn wsmpld.api:app --host 127.0.0.1 --port 8000" in readme
    )
    assert "http://127.0.0.1:8000/docs" in readme
    assert "python -m pytest" in readme
    assert 'python -m pytest -m live tests/test_live_samples.py -q' in readme
    assert "python -m ruff check ." in readme
    assert "python -m mypy" in readme
    assert "python -m diagnostics.camoufox_clearance" in readme
    assert "python -m diagnostics.cookie_reuse" in readme
