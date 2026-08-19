from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from diagnostics import camoufox_clearance, cookie_reuse


@pytest.mark.parametrize("diagnostic", [camoufox_clearance, cookie_reuse])
@pytest.mark.parametrize(("succeeded", "exit_code"), [(True, 0), (False, 1)])
def test_manual_diagnostic_exit_status_reports_its_result(
    monkeypatch: pytest.MonkeyPatch,
    diagnostic: ModuleType,
    succeeded: bool,
    exit_code: int,
) -> None:
    def run_diagnostic() -> bool:
        return succeeded

    monkeypatch.setattr(diagnostic, "run_diagnostic", run_diagnostic)

    with pytest.raises(SystemExit) as exit_result:
        diagnostic.main()

    assert exit_result.value.code == exit_code


@pytest.mark.parametrize("status_code", [404, 500])
def test_cookie_reuse_diagnostic_rejects_non_success_response(
    monkeypatch: pytest.MonkeyPatch,
    status_code: int,
) -> None:
    class FakeSession:
        def __enter__(self) -> "FakeSession":
            return self

        def __exit__(self, *args: object) -> None:
            pass

        def get(self, *args: object, **kwargs: object) -> SimpleNamespace:
            return SimpleNamespace(
                status_code=status_code,
                text="Kanye-West appears in this upstream error page",
            )

    def fake_session(*args: object, **kwargs: object) -> FakeSession:
        return FakeSession()

    cookies: list[dict[str, Any]] = [
        {"name": "cf_clearance", "value": "do-not-log-this-secret"}
    ]

    def solve_with_browser(artist: str) -> tuple[list[dict[str, Any]], str]:
        return cookies, "test-agent"

    monkeypatch.setattr(cookie_reuse, "_solve_with_browser", solve_with_browser)
    monkeypatch.setattr(cookie_reuse.cf_requests, "Session", fake_session)

    assert cookie_reuse.run_diagnostic() is False
