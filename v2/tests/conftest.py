"""Every test gets its own state directory (no test can write data/*.db inside the repo) and leaves the
shared settings, valuation rates and Broward switch exactly as it found them (tenant profiles mutate them)."""

import dataclasses

import pytest

from lead_engine import broward, valuation
from lead_engine.config import settings


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    saved = {f.name: getattr(settings, f.name) for f in dataclasses.fields(settings)}
    rates = dict(valuation.RATES)
    monkeypatch.delenv("INSPECTORS", raising=False)
    object.__setattr__(settings, "state_db", tmp_path / "state" / "state.db")
    yield
    for k, v in saved.items():
        object.__setattr__(settings, k, v)
    valuation.RATES.clear()
    valuation.RATES.update(rates)
    broward.reset()
