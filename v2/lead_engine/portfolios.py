"""Portfolio dossiers per management firm (`python -m lead_engine portfolios --top 15 --pdf`).

1. Ranking (whole synced DBPR registry, offline): condos grouped by firm identity (CAB licence when
   the CAM registry knows the firm, else its name key) inside CLUSTER_COUNTIES; per firm: buildings
   (one per street address), units, combined annual contract estimate (valuation.py from units and
   coastal ZIP), cities. Ranked by the high end of the combined estimate.
2. Live signals for the top firms: their `depth` largest buildings are looked up in the Miami-Dade
   cadastre (year built, floors) and the municipal layers (recertification, violations, permits) to
   count buildings in the milestone / recertification window and with open records, and to list
   the most urgent properties.
3. Output in out/portfolios/<date>/: ranking.csv, ranking.json (with each firm's buildings) and,
   with --pdf, a 2-page executive dossier per firm (portfolio KPIs, most urgent properties,
   Preferred Vendor Program proposal with the tenant's tiers, QR to book).
"""

from __future__ import annotations

import csv
import json
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from loguru import logger

import re

from . import appraiser, mgmt_domains, municipal_signals, valuation
from .config import settings
from .dbpr import _short_condo_name, _title_case, address_key, connect

COASTAL_ZIPS = None


def _coastal(zip_code: str | None) -> bool:
    global COASTAL_ZIPS
    if COASTAL_ZIPS is None:
        from .pipeline import COASTAL_3MI_ZIPS
        COASTAL_ZIPS = COASTAL_3MI_ZIPS
    return (zip_code or "") in COASTAL_ZIPS


LIVE_COUNTIES = {"DADE", "MIAMI-DADE"}          # Property Appraiser + municipal layers coverage


def display(name: str) -> str:
    """Readable firm name: catalog spelling when known (FirstService, KW), vowel-less acronyms upper-cased."""
    words = name.split()
    hit = mgmt_domains.from_catalog(name)
    if hit:
        target = re.sub(r"\W", "", hit[0]).lower()
        for k in range(1, len(words) + 1):
            if re.sub(r"\W", "", "".join(words[:k])).lower() == target:
                return " ".join([hit[0]] + words[k:])         # keeps the legal suffix (LLC, INC)
    abbrev = {"MGMT", "MGT", "ASSN", "SVCS", "SVC", "CTR", "MNGMT"}
    return " ".join(w.upper() if not re.search(r"[aeiouy]", w, re.I) and re.sub(r"\W", "", w).upper() not in abbrev
                    else w for w in words)


def firm_name(firm_id: str, names: Counter, conn: sqlite3.Connection) -> tuple[str, str | None]:
    """(display name, licence id) for a firm id."""
    if firm_id.startswith("LIC:"):
        lic = firm_id[4:]
        try:
            row = conn.execute("SELECT name, dba FROM cam_licenses WHERE license_id = ?", (lic,)).fetchone()
        except sqlite3.Error:
            row = None
        if row:
            return _title_case(row["dba"] or row["name"]), lic
        return names.most_common(1)[0][0], lic
    return names.most_common(1)[0][0], None


def rank(conn: sqlite3.Connection | None = None, counties: list[str] | None = None,
         cities: list[str] | None = None) -> list[dict[str, Any]]:
    """Firms ranked by the buildings they manage *inside the service area* (tenant / TARGET_CITIES, with the
    neighbourhood ZIP aliases: Brickell → 33129-33131, Sunny Isles → 33160…). Buildings outside it are
    not counted in buildings, units, estimate or dossier."""
    from .collectors.dbpr_seed import area_filter, in_area
    from .config import service_area

    conn = conn or connect()
    area = area_filter(cities or service_area())
    counties = [c.upper() for c in (counties or settings.cluster_counties)]
    marks = ",".join("?" * len(counties))
    rows = conn.execute(
        f"SELECT r.firm_id, r.company, c.project_number, c.condo_name, c.address, c.zip, c.units, c.county, r.city "
        f"FROM dbpr_routes r JOIN dbpr_condos c USING (project_number) "
        f"WHERE r.firm_id IS NOT NULL AND c.primary_status = 'Approved' AND UPPER(c.county) IN ({marks})",
        counties).fetchall()
    firms: dict[str, dict[str, Any]] = {}
    for r in rows:
        if not in_area(r["city"], r["zip"], area):
            continue
        f = firms.setdefault(r["firm_id"], {"names": Counter(), "buildings": {}})
        f["names"][r["company"]] += 1
        key = address_key(r["address"]) or r["project_number"]
        b = f["buildings"].get(key)
        if b is None:
            f["buildings"][key] = {"name": _short_condo_name(r["condo_name"]), "address": r["address"], "zip": r["zip"],
                                   "city": (r["city"] or "").title(), "county": r["county"], "units": r["units"] or 0,
                                   "project_number": r["project_number"]}
        else:
            b["units"] += r["units"] or 0
    # One firm, several registry spellings that the CAM licence does not join (e.g. "Castle Group" and the
    # licensed "Castle Management LLC"): merged when the management-domain catalog maps both to one company.
    by_catalog: dict[str, str] = {}
    for fid in sorted(firms, key=lambda k: (not k.startswith("LIC:"), -len(firms[k]["buildings"]))):
        hit = mgmt_domains.from_catalog(firms[fid]["names"].most_common(1)[0][0])
        if not hit:
            continue
        keep = by_catalog.setdefault(hit[1], fid)
        if keep != fid:
            src, dst = firms.pop(fid), firms[keep]
            dst["names"].update(src["names"])
            for k, b in src["buildings"].items():
                dst["buildings"].setdefault(k, b)
            dst.setdefault("merged", []).append(src["names"].most_common(1)[0][0])
    out = []
    for fid, f in firms.items():
        name, lic = firm_name(fid, f["names"], conn)
        hit = mgmt_domains.from_catalog(name)
        if f.get("merged") and hit:
            name = hit[0]
        name = display(name)
        lo = hi = 0
        blds = list(f["buildings"].values())
        for b in blds:
            est = valuation.estimate({"units": b["units"], "coastal": _coastal(b["zip"])}) if b["units"] else None
            b["est_low"], b["est_high"] = (est["low"], est["high"]) if est else (0, 0)
            lo += b["est_low"]
            hi += b["est_high"]
        out.append({
            "firm_id": fid, "firm": name, "license": lic, "buildings": len(blds),
            "registry_names": [n for n, _ in f["names"].most_common(5)],
            "units": sum(b["units"] for b in blds), "total_estimated_low": lo, "total_estimated_usd": hi,
            "cities": [c for c, _ in Counter(b["city"] for b in blds).most_common(4)],
            "building_list": sorted(blds, key=lambda b: -b["units"]),
        })
    return sorted(out, key=lambda f: (-f["total_estimated_usd"], -f["buildings"]))


def analyze_live(firm: dict[str, Any], depth: int = 10) -> dict[str, Any]:
    """Cadastre + municipal signals for the firm's `depth` largest buildings where the official layers
    have coverage (Miami-Dade); `analyzed` counts only buildings the cadastre actually returned."""
    milestone = with_records = analyzed = 0
    from .broward import allowed_zips
    east = allowed_zips()
    covered = [b for b in firm["building_list"] if (b.get("county") or "").upper() in LIVE_COUNTIES
               or ((b.get("county") or "").upper() == "BROWARD" and (b.get("zip") or "")[:5] in east)][:depth]
    for b in covered:
        addr = b["address"]
        cad = appraiser.lookup(addr) or {}
        sigs = municipal_signals.lookup(addr)
        ms = appraiser.milestone_signal(cad.get("year_built"), cad.get("floors"), _coastal(b["zip"]),
                                        units=b["units"], condo=True)
        analyzed += bool(cad)
        b.update({"year_built": cad.get("year_built"), "floors": cad.get("floors"),
                  "signals": ([ms] if ms else []) + sigs})
        b["urgency"] = sum(s["weight"] for s in b["signals"]) + b["units"] / 200
        milestone += bool(ms)
        with_records += bool(sigs)
    firm.update({"analyzed": analyzed, "milestone_window": milestone, "municipal_records": with_records,
                 "live_checked": len(covered)})
    flagged = sorted([b for b in covered if b.get("signals")], key=lambda b: -b["urgency"])
    rest = [b for b in firm["building_list"] if b not in flagged]
    firm["urgent"] = (flagged + rest)[:10]
    return firm


def export(firms: list[dict[str, Any]], out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path, json_path = out_dir / "ranking.csv", out_dir / "ranking.json"
    with open(csv_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["rank", "firm", "license", "buildings", "units", "total_estimated_low", "total_estimated_usd",
                    "analyzed", "milestone_window", "municipal_records", "cities"])
        for i, f in enumerate(firms, 1):
            w.writerow([i, f["firm"], f["license"] or "", f["buildings"], f["units"], f["total_estimated_low"],
                        f["total_estimated_usd"], f.get("analyzed", ""), f.get("milestone_window", ""),
                        f.get("municipal_records", ""), "; ".join(f["cities"])])
    json_path.write_text(json.dumps(firms, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return csv_path, json_path


def run(top: int = 15, depth: int = 10, live: bool = True, pdf: bool = False, lang: str | None = None,
        out_dir: Path | None = None, conn: sqlite3.Connection | None = None) -> Path:
    firms = rank(conn)[:top]
    if live:
        appraiser.reset()
        municipal_signals.reset()
        for i, f in enumerate(firms, 1):
            logger.info("[{}/{}] señales en vivo para {} ({} edificios)", i, len(firms), f["firm"], f["buildings"])
            analyze_live(f, depth)
    day = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
    out = Path(out_dir or settings.out_dir) / "portfolios" / day
    export(firms, out)
    if pdf:
        from .portfolio_pdf import build
        from .pdf_common import safe_filename
        for f in firms:
            build(f, out / "pdf" / f"{safe_filename(f['firm'])}.pdf", lang=lang)
        from .batch_report import build_portfolios
        build_portfolios(out, firms)
    return out


def print_table(firms: list[dict[str, Any]]) -> None:
    print(f"{'#':>2}  {'Administradora':<42} {'Edif.':>5} {'Unid.':>7} {'US$/año (alto)':>15} {'Milestone':>9} {'Registros':>9}")
    for i, f in enumerate(firms, 1):
        print(f"{i:>2}  {f['firm'][:42]:<42} {f['buildings']:>5} {f['units']:>7} {f['total_estimated_usd']:>15,} "
              f"{str(f.get('milestone_window', '-')):>9} {str(f.get('municipal_records', '-')):>9}")
