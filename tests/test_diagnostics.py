from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from diagnostics import camoufox_clearance, cookie_reuse
from wsmpld.upstream import ClearanceSession

FIREFOX_135_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:135.0) "
    "Gecko/20100101 Firefox/135.0"
)


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


def test_cookie_reuse_diagnostic_reports_safe_handoff_details(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    selected_profiles: list[str] = []

    class FakeSession:
        def __init__(self, impersonate: str) -> None:
            selected_profiles.append(impersonate)

        def __enter__(self) -> "FakeSession":
            return self

        def __exit__(self, *args: object) -> None:
            pass

        def get(self, *args: object, **kwargs: object) -> SimpleNamespace:
            return SimpleNamespace(
                status_code=200,
                text="Kanye-West",
                url="https://www.whosampled.com/Kanye-West/",
            )

    cookies: list[dict[str, Any]] = [
        {"name": "cf_clearance", "value": "do-not-log-clearance"},
        {"name": "session", "value": "do-not-log-session"},
    ]
    monkeypatch.setattr(
        cookie_reuse,
        "_solve_with_browser",
        lambda artist: (cookies, FIREFOX_135_USER_AGENT),
    )
    monkeypatch.setattr(cookie_reuse.cf_requests, "Session", FakeSession)

    assert cookie_reuse.run_diagnostic() is True

    output = capsys.readouterr().out
    assert "browser major: 135" in output
    assert "impersonation profile: firefox135" in output
    assert "cookie names: cf_clearance, session" in output
    assert "resolved host: www.whosampled.com" in output
    assert "status: 200" in output
    assert "challenged: False" in output
    assert selected_profiles == ["firefox135"]
    assert "do-not-log-clearance" not in output
    assert "do-not-log-session" not in output


def test_cookie_reuse_diagnostic_uses_production_clearance_acquirer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    acquisition_timeouts: list[float] = []
    clearance = ClearanceSession(
        cookies={"cf_clearance": "do-not-log-clearance"},
        user_agent=FIREFOX_135_USER_AGENT,
        expires_at=10_000.0,
    )

    class FakeClearanceAcquirer:
        def __call__(self, timeout: float) -> ClearanceSession:
            acquisition_timeouts.append(timeout)
            return clearance

    monkeypatch.setattr(
        cookie_reuse,
        "CamoufoxClearanceAcquirer",
        FakeClearanceAcquirer,
    )

    cookies, user_agent = cookie_reuse._solve_with_browser("Structure")

    assert acquisition_timeouts == [90.0]
    assert cookies == [
        {"name": "cf_clearance", "value": "do-not-log-clearance"}
    ]
    assert user_agent == FIREFOX_135_USER_AGENT
