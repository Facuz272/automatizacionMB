"""Every test gets its own state directory (no test can write data/*.db inside the repo) and leaves the
shared settings, valuation rates and Broward switch exactly as it found them (tenant profiles mutate them)."""

import dataclasses

import pytest

from lead_engine import broward, valuation
from lead_engine.config import settings


@pytest.fixture(autouse=True)
def _no_firm_site_crawl(monkeypatch):
    """Plan B looks up the management firm's published phone; tests never crawl real firm sites.
    Email checks never fall back to DNS-over-HTTPS (tests that need it simulate it)."""
    from lead_engine import email_verify, phone_resolver
    monkeypatch.setattr(phone_resolver, "firm_site_phone", lambda domain: None)

    def no_doh(*a, **k):
        raise email_verify._NoDNS("DoH disabled in tests")
    monkeypatch.setattr(email_verify, "_doh", no_doh)
    email_verify.backend.cache_clear()
    email_verify.domain_status.cache_clear()
    yield
    for fn in (email_verify.backend, email_verify.domain_status):     # tests may have replaced them
        getattr(fn, "cache_clear", lambda: None)()


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
