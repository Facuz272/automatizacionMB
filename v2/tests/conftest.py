"""Every test gets its own state directory: no test can write data/*.db inside the repo."""

import pytest

from lead_engine.config import settings


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    original = settings.state_db
    object.__setattr__(settings, "state_db", tmp_path / "state" / "state.db")
    yield
    object.__setattr__(settings, "state_db", original)
