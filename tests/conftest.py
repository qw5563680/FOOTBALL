import pytest
import responses


@pytest.fixture(autouse=True, scope="session")
def forbid_live_http():
    """An unregistered requests call must fail even outside a test's local mock."""
    with responses.RequestsMock():
        yield


@pytest.fixture
def sfc_fixture_clock(monkeypatch):
    """Judge the lottery period sequence at the time the archived sample pages were
    current (period 26156 closed on 2026-11-02), not at the real clock."""
    from datetime import UTC, datetime

    import zucai.sources.sfc as sfc

    monkeypatch.setattr(sfc, "fetch_official_notices", lambda: [])
    monkeypatch.setattr(
        sfc, "_sequence_time", lambda meta: datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
    )
