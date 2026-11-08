from lead_engine import costs, search

RESULTS = [
    {"title": "Oceania II Condominium - FirstService Residential", "url": "https://fsresidential.com/florida/oceania-ii",
     "snippet": "Oceania II Condo in Sunny Isles is managed by FirstService Residential. Contact Maria Lopez, LCAM at mlopez@fsrmail.com."},
    {"title": "Oceania Towers rentals", "url": "https://zillow.com/x", "snippet": "Apartments for rent near Oceania."},
    {"title": "Some other building", "url": "https://castlegroup.com/z",
     "snippet": "Harbor Point is managed by Castle Group. John Doe, Property Manager."},
]


def test_query_shape():
    q = search.build_query("Oceania II", "Sunny Isles Beach")
    assert q == '"Oceania II" "Sunny Isles Beach" ("property manager" OR "LCAM" OR "managed by" OR "condominium association")'
    assert search.city_of("16485 Collins Ave, Sunny Isles Beach, FL 33160") == "Sunny Isles Beach"


def test_analyze_only_trusts_results_that_name_the_building():
    a = search.analyze(RESULTS, "Oceania II")
    assert [h["url"] for h in a["hits"]] == ["https://fsresidential.com/florida/oceania-ii"]
    assert a["companies"][0][0] == "FirstService Residential"
    assert {"name": "Maria Lopez", "title": "LCAM"}.items() <= a["people"][0].items()
    assert a["emails"] == ["mlopez@fsrmail.com"]
    assert all("Castle" not in c for c, _ in a["companies"])   # Harbor Point's manager is not attributed


def test_provider_selection_and_serpapi_call(monkeypatch):
    from lead_engine import config
    s = type(config.settings)(**{**config.settings.__dict__, "search_provider": "auto", "serpapi_key": None,
                                 "google_cse_key": None, "google_cse_cx": None, "brave_search_key": None})
    monkeypatch.setattr(search, "settings", s)
    assert search.provider() is None and search.discover("X", "Miami") == {}
    s2 = type(s)(**{**s.__dict__, "serpapi_key": "k", "brave_search_key": "b"})
    monkeypatch.setattr(search, "settings", s2)
    assert search.provider() == "serpapi"
    calls = []

    class Resp:
        status_code = 200

        def json(self):
            return {"organic_results": [{"title": "t", "link": "https://a.com", "snippet": "s"}]}

    monkeypatch.setattr(search.requests, "get", lambda url, timeout, **kw: calls.append((url, kw)) or Resp())
    costs.tracker.reset()
    assert search.web_search("q") == [{"title": "t", "url": "https://a.com", "snippet": "s"}]
    assert calls[0][0] == "https://serpapi.com/search.json" and calls[0][1]["params"]["api_key"] == "k"
    assert costs.tracker.counts["serpapi"] == 1
