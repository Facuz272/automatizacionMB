"""Sunbiz Corporate Data File: official fixed-width layout, CSV, legal-name normalisation and matching
against DBPR association names, officers injected as contacts (never an address)."""

import io
import json
import zipfile

import pytest

from lead_engine import pipeline, sunbiz
from lead_engine.models import Lead


def fixed(doc, name, *, status="A", ftype="DOMNP", city="MIAMI BEACH", state="FL", zip5="33140",
          ra=("SMITH LAW PA", "C"), officers=(), file_date="19870312"):
    """One 1440-character record laid out exactly as dos.sunbiz.org/data-definitions/cor.html says."""
    line = [" "] * sunbiz.RECORD_LEN

    def put(start, length, value):
        v = str(value)[:length].ljust(length)
        line[start - 1:start - 1 + length] = list(v)
    put(1, 12, doc); put(13, 192, name); put(205, 1, status); put(206, 15, ftype)          # noqa: E702
    put(221, 42, "5600 COLLINS AVE"); put(305, 28, city); put(333, 2, state); put(335, 10, zip5)  # noqa: E702
    put(473, 8, file_date); put(545, 42, ra[0]); put(587, 1, ra[1])                         # noqa: E702
    put(588, 42, "100 RA HOME ST")                                                          # agent address
    for i, (title, kind, oname, addr) in enumerate(officers):
        base = 669 + i * 128
        put(base, 4, title); put(base + 4, 1, kind); put(base + 5, 42, oname)               # noqa: E702
        put(base + 47, 42, addr); put(base + 89, 28, "MIAMI"); put(base + 117, 2, "FL")      # noqa: E702
        put(base + 119, 9, "331401234")
    return "".join(line)


OLYMPUS = fixed("N05000001234", "OLYMPUS ASSOCIATION, INC.", officers=[
    ("P", "P", "RUIZ, ANA", "12 RESIDENT HOME DR"), ("VP", "P", "PAZ, JUAN", "34 RESIDENT HOME DR"),
    ("T", "P", "LEE, KIM", "56 RESIDENT HOME DR"), ("D", "C", "CASTLE GROUP LLC", "1 FIRM WAY")])


def test_layout_positions_match_the_official_definition():
    rec = sunbiz.parse_fixed(OLYMPUS)
    assert len(OLYMPUS) == 1440
    assert rec["doc_number"] == "N05000001234" and rec["name"] == "OLYMPUS ASSOCIATION, INC."
    assert (rec["status"], rec["filing_type"], rec["city"], rec["zip"]) == ("A", "DOMNP", "MIAMI BEACH", "33140")
    assert rec["ra_name"] == "SMITH LAW PA" and rec["ra_type"] == "C"
    assert [(o["title_code"], o["kind"], o["name"]) for o in rec["officers"]] == [
        ("P", "P", "RUIZ, ANA"), ("VP", "P", "PAZ, JUAN"), ("T", "P", "LEE, KIM"), ("D", "C", "CASTLE GROUP LLC")]
    assert "RESIDENT" not in json.dumps(rec) and "RA HOME" not in json.dumps(rec)     # no addresses parsed


@pytest.mark.parametrize("dbpr_name,sunbiz_name", [
    ("OLYMPUS ASSOC., INC.", "OLYMPUS ASSOCIATION, INC."),
    ("HEMISPHERES CONDO ASSN INC.", "HEMISPHERES CONDOMINIUM ASSOCIATION, INC."),
    ("The Oceanside Plaza Condominium Ass'n, Inc", "OCEANSIDE PLAZA CONDOMINIUM ASSOCIATION, INC."),
    ("Point East Condo. Assoc. Inc", "POINT EAST CONDOMINIUM ASSOCIATION, INCORPORATED"),
    ("BAY HARBOR TWRS CONDO ASSN NO. 2, INC.", "BAY HARBOR TOWERS CONDOMINIUM ASSOCIATION NO 2, INC."),
    ("Sea & Sky Condominiums Association, Inc.", "SEA AND SKY CONDOMINIUM ASSOCIATION INC"),
])
def test_legal_names_normalise_to_the_same_key(dbpr_name, sunbiz_name):
    assert sunbiz.legal_key(dbpr_name) == sunbiz.legal_key(sunbiz_name)


@pytest.mark.parametrize("a,b", [
    ("OLYMPUS ASSOC., INC.", "OLYMPIA ASSOCIATION, INC."),
    ("BAY HARBOR TOWERS CONDO ASSN NO. 1", "BAY HARBOR TOWERS CONDO ASSN NO. 2"),
    ("OCEAN TOWER MASTER ASSOCIATION", "OCEAN TOWER CONDOMINIUM ASSOCIATION"),
])
def test_different_associations_never_collapse(a, b):
    assert sunbiz.legal_key(a) != sunbiz.legal_key(b)


@pytest.mark.parametrize("code,title", [("P", "President"), ("VP", "Vice President"), ("PD", "President / Director"),
                                        ("VPD", "Vice President / Director"), ("STD", "Secretary / Treasurer / Director"),
                                        ("TREA", "Treasurer"), ("MGR", "Manager"), ("PM", "Property Manager"),
                                        ("AMBR", "AMBR"), ("", None)])
def test_officer_titles(code, title):
    assert sunbiz.officer_title(code) == title


def _db(tmp_path, *lines, csv_text=None):
    folder = tmp_path / "sunbiz"
    folder.mkdir()
    (folder / "cordata0.txt").write_text("\r\n".join(lines) + "\r\n", encoding="latin-1")
    if csv_text:
        (folder / "extra.csv").write_text(csv_text, encoding="utf-8")
    conn = sunbiz.connect(tmp_path / "sunbiz.db")
    return conn, sunbiz.sync(folder, conn)


def test_sync_indexes_only_associations_and_never_addresses(tmp_path):
    shop = fixed("P12000000001", "JOE'S PIZZA LLC", ftype="FLAL")
    conn, stats = _db(tmp_path, OLYMPUS, shop)
    assert stats == {"files": 1, "read": 2, "kept": 1, "officers": 4}
    dump = json.dumps([list(r) for t in ("sunbiz_corps", "sunbiz_officers")
                       for r in conn.execute(f"SELECT * FROM {t}").fetchall()])
    assert "RESIDENT" not in dump and "RA HOME" not in dump and "COLLINS" not in dump and "PIZZA" not in dump


def test_match_by_dbpr_legal_name_and_zip(tmp_path):
    old = fixed("N90000000777", "OLYMPUS ASSOC., INC.", status="I", zip5="33139", file_date="19900101")
    conn, _ = _db(tmp_path, OLYMPUS, old)
    hit = sunbiz.match("OLYMPUS ASSOC., INC.", "33140", "MIAMI BEACH", conn=conn)
    assert hit["doc_number"] == "N05000001234" and hit["active"] and hit["matched_by"] == "name+zip"
    assert [o["title"] for o in hit["officers"]] == ["President", "Vice President", "Treasurer", "Director"]
    assert hit["officers"][0]["name"] == "Ana Ruiz" and hit["registered_agent"] == {"name": "SMITH LAW PA", "kind": "C"}
    assert sunbiz.match("NOT THERE CONDO ASSN", "33140", conn=conn) is None


def test_close_name_needs_the_same_zip_or_city(tmp_path):
    conn, _ = _db(tmp_path, fixed("N1", "THE CARRIAGE CLUB NORTH CONDOMINIUM ASSOCIATION, INC.", zip5="33160",
                                  city="SUNNY ISLES BEACH"))
    hit = sunbiz.match("CARRIAGE CLUB NORTH ASSN INC", "33160", conn=conn)       # "CONDOMINIUM" missing in DBPR
    assert hit and hit["matched_by"].startswith("name~")
    assert sunbiz.match("CARRIAGE CLUB NORTH ASSN INC", "33139", "MIAMI", conn=conn) is None


def test_csv_and_zip_files_are_read(tmp_path):
    csv_text = ("Document Number,Corporation Name,Status,Filing Type,Principal City,Principal Zip,"
                "Registered Agent Name,Registered Agent Type,officer1_title,officer1_name,officer1_type,officer1_address\n"
                "N22000000042,BLUE TOWER CONDOMINIUM ASSN INC,A,DOMNP,MIAMI,33132,\"DOE, JANE\",P,P,\"GARCIA, LUIS\",P,"
                "9 PRIVATE HOME\n")
    conn, stats = _db(tmp_path, OLYMPUS, csv_text=csv_text)
    zpath = tmp_path / "sunbiz" / "quarterly.zip"
    with zipfile.ZipFile(zpath, "w") as z:
        z.writestr("cordata5.txt", fixed("N33", "SEA AND SKY CONDOMINIUM ASSOCIATION INC", zip5="33139") + "\n")
    stats = sunbiz.sync(tmp_path / "sunbiz", conn)
    assert stats["files"] == 3 and stats["kept"] == 3
    blue = sunbiz.match("Blue Tower Condo Assoc., Inc.", "33132", conn=conn)
    assert blue["officers"] == [{"title": "President", "name": "Luis Garcia", "kind": "P"}]
    assert blue["registered_agent"]["name"] == "Jane Doe"
    assert "PRIVATE HOME" not in json.dumps([list(r) for r in conn.execute("SELECT * FROM sunbiz_officers")])
    assert sunbiz.match("SEA & SKY CONDOS ASSN", "33139", conn=conn)["doc_number"] == "N33"


def test_no_local_index_means_no_lookup(tmp_path):
    assert not sunbiz._db_path().exists()
    assert sunbiz.match("OLYMPUS ASSOC., INC.", "33140") is None
    assert not sunbiz._db_path().exists()                            # matching never creates an empty db


def _lead(**kw):
    l = Lead(source="dbpr", source_id="dbpr:4410", strategy_kind="onsite", company_name="Olympus",
             address="5600 Collins Ave, Miami Beach, FL 33140", phone="+13054278770")
    l.public_records["dbpr"] = {"association": "Olympus Assoc., Inc."}
    for k, v in kw.items():
        setattr(l, k, v)
    return l


def test_officers_become_contact_and_plan_b(tmp_path):
    folder = sunbiz.data_dir()
    folder.mkdir(parents=True)
    (folder / "cordata0.txt").write_text(OLYMPUS + "\n", encoding="latin-1")
    sunbiz.sync(folder)
    lead = _lead()
    pipeline.apply_sunbiz(lead)
    assert lead.public_records["sunbiz"]["doc_number"] == "N05000001234"
    assert (lead.contact_name, lead.contact_title) == ("Ana Ruiz", "President of the Board (Sunbiz)")
    assert lead.evidence["contact_from"] == "sunbiz"
    assert "Castle Group" not in json.dumps(lead.contacts)              # corporate officers are not people
    brief = lead.brief()
    assert "Plan B: Vice President of the Board (Sunbiz) Juan Paz" in brief
    assert "RESIDENT" not in json.dumps(lead.internal_record())
    # a known property manager stays the contact; officers only add Plan B options
    pm = _lead(contact_name="Laura Gomez", contact_title="Property Manager")
    pipeline.apply_sunbiz(pm)
    assert pm.contact_name == "Laura Gomez" and any(c["name"] == "Ana Ruiz" for c in pm.contacts)


def test_sunbiz_sync_cli_without_files(capsys):
    from lead_engine.__main__ import main
    assert main(["sunbiz-sync"]) == 1
    assert "No hay archivos" in capsys.readouterr().out
