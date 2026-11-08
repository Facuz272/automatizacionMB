"""Importer against a fake transport that mimics /api/public/leads-import."""

import pytest

from lead_engine import crm_importer
from lead_engine.crm_importer import CrmAuthError, CrmClient, import_leads, prepare
from lead_engine.normalize import phone_key


class FakeResp:
    def __init__(self, status, body):
        self.status_code, self._body, self.text = status, body, str(body)

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


class FakeSession:
    """Existing CRM phones + per-batch behaviour like public.import_leads."""

    def __init__(self, existing=(), fail_first=0, status=200):
        self.existing = {phone_key(p) for p in existing}
        self.calls = []
        self.fail_first = fail_first
        self.status = status

    def post(self, url, json, headers, timeout):
        self.calls.append({"url": url, "body": json, "headers": headers})
        if self.fail_first:
            self.fail_first -= 1
            return FakeResp(503, {"error": "down"})
        if self.status != 200:
            return FakeResp(self.status, {"error": "unauthorized"})
        dups, n = [], 0
        for i, lead in enumerate(json["leads"]):
            if phone_key(lead["phone"]) in self.existing:
                dups.append({"index": i, "existing_position": 7, "existing_company": "Old"})
            else:
                n += 1
                if not json["dry_run"]:
                    self.existing.add(phone_key(lead["phone"]))
        return FakeResp(200, {"ok": True, "dry_run": json["dry_run"], "inserted": 0 if json["dry_run"] else n,
                              "would_insert": n, "positions": {"from": 1, "to": n} if n else None,
                              "duplicates": dups, "rejected": []})


def _client(session):
    c = CrmClient(base_url="https://crm.test", secret="s3cret")
    c.session = session
    return c


def _leads(n, start=0):
    return [{"company_name": f"Condo {i}", "phone": f"+1305861{4000 + i:04d}", "segment": "Gran Comunidad"}
            for i in range(start, start + n)]


def test_batches_of_200_and_global_indexes(monkeypatch):
    monkeypatch.setattr(crm_importer.time, "sleep", lambda s: None)
    leads = _leads(450)
    sess = FakeSession(existing=[leads[300]["phone"]])
    rep = import_leads(leads, "b1", dry_run=True, client=_client(sess))
    assert [len(c["body"]["leads"]) for c in sess.calls] == [200, 200, 50]
    assert all(c["headers"]["x-webhook-secret"] == "s3cret" for c in sess.calls)
    assert all(c["url"] == "https://crm.test/api/public/leads-import" for c in sess.calls)
    assert all(c["body"]["import_batch_id"] == "b1" and c["body"]["source"] == "lead-engine-v2" for c in sess.calls)
    assert rep.would_insert == 449 and rep.inserted == 0
    assert rep.duplicates[0]["index"] == 300 and rep.duplicates[0]["company_name"] == "Condo 300"


def test_commit_and_retry_on_5xx(monkeypatch):
    monkeypatch.setattr(crm_importer.time, "sleep", lambda s: None)
    sess = FakeSession(fail_first=2)
    rep = import_leads(_leads(3), "b2", dry_run=False, client=_client(sess))
    assert rep.inserted == 3 and len(sess.calls) == 3
    # re-sending is idempotent thanks to phone_key dedup on the server
    rep2 = import_leads(_leads(3), "b2", dry_run=False, client=_client(sess))
    assert rep2.inserted == 0 and len(rep2.duplicates) == 3


def test_unauthorized_raises():
    with pytest.raises(CrmAuthError):
        import_leads(_leads(1), "b3", client=_client(FakeSession(status=401)))


def test_prepare_skips_missing_and_repeated():
    rows = _leads(2) + [{"company_name": "X", "phone": "(305) 861-4000", "segment": "Gran Comunidad"},
                        {"company_name": "Y", "phone": "+13058619999"}]
    ok, skipped = prepare(rows)
    assert [i for i, _ in ok] == [0, 1]
    assert {s["index"] for s in skipped} == {2, 3}


def test_precheck_returns_existing_keys(monkeypatch):
    sess = FakeSession(existing=["+13058614001"])
    keys = crm_importer.existing_phone_keys(_leads(3), _client(sess))
    assert keys == {"3058614001"}
    assert sess.calls[0]["body"]["dry_run"] is True
    assert "import_batch_id" not in sess.calls[0]["body"]
