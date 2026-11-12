"""CLI.

  python -m lead_engine run --strategy onsite --limit 20 --dry-run
  python -m lead_engine run --strategy portfolio --limit 50 --commit
  python -m lead_engine run --source csv --seed dbpr_condos.csv --strategy onsite --limit 20 --dry-run
  python -m lead_engine import out/<lote>/crm_payload.json [--commit]
  python -m lead_engine run --seed dbpr --strategy onsite --limit 20 --dry-run   # semilla desde el padrón DBPR
  python -m lead_engine dbpr-sync [--groups MD,PB]         # padrón de condominios + licencias CAM (DBPR)
  python -m lead_engine dbpr-load Condo_MD.csv [...]       # o cargar CSVs ya descargados
  python -m lead_engine --tenant mb_softwash run --seed dbpr --limit 20 --pdf --pdf-lang en
  python -m lead_engine portfolios --top 15 --pdf          # ranking de administradoras + dossiers PDF
  python -m lead_engine enrich-phones out/<lote>/needs_phone.csv --dry-run   # reintentar teléfonos pendientes
  python -m lead_engine daily --target-pool 40 [--commit]  # rutina diaria completa
  python -m lead_engine feedback [--metrics]               # resultados del CRM → teléfonos malos, conversión
  python -m lead_engine sunbiz-sync                         # directivos de asociaciones (data/sunbiz/)
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from loguru import logger

from . import cam, crm_importer, dbpr, lodging, pipeline, search
from .config import settings


def main(argv: list[str] | None = None) -> int:
    from .runlock import RunLocked

    try:
        return _main(argv)
    except RunLocked as exc:
        logger.error("{}", exc)
        print(str(exc), file=sys.stderr)
        return 3


def _main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="lead_engine", description="MB Lead Engine v2")
    ap.add_argument("--tenant", help="perfil de empresa en tenants/<slug>.json (default TENANT)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="recolectar, validar, enriquecer y (opcional) enviar al CRM")
    r.add_argument("--strategy", choices=["onsite", "pain", "portfolio"], default="onsite")
    r.add_argument("--limit", type=int, default=20, help="leads enriquecidos a producir")
    r.add_argument("--source", action="append", choices=["places", "osm", "csv", "dbpr", "npi", "pa"],
                   help="repetible; por defecto places si hay GOOGLE_PLACES_API_KEY, si no dbpr")
    r.add_argument("--seed", help="dbpr (condominios) | npi (clínicas) | pa (catastro: --vertical) | archivo.csv")
    r.add_argument("--vertical", choices=["plaza", "commercial", "clinic"], help="--seed pa: segmento a sembrar")
    r.add_argument("--min-units", type=int, help="semilla DBPR: mínimo de unidades (default MIN_UNITS=50)")
    r.add_argument("--max-candidates", type=int, default=None, help="tope de candidatos crudos (default 6×limit)")
    r.add_argument("--no-ai", action="store_true", help="solo reglas, sin Claude")
    r.add_argument("--no-apollo", action="store_true")
    r.add_argument("--no-precheck", action="store_true", help="no consultar al CRM por duplicados antes de enriquecer")
    r.add_argument("--no-search", action="store_true", help="sin búsqueda web externa (SerpAPI/CSE/Brave)")
    r.add_argument("--no-geo", action="store_true", help="sin perfil físico de OpenStreetMap")
    r.add_argument("--no-dbpr", action="store_true", help="sin cruce con el padrón DBPR")
    r.add_argument("--no-mx", action="store_true", help="sin validación DNS/MX de emails")
    r.add_argument("--render", choices=["off", "auto", "always"], help="renderizado JS con Playwright (default RENDER_JS)")
    mode = r.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true",
                      help="envía al CRM con dry_run=true (no escribe) y guarda la respuesta")
    mode.add_argument("--commit", action="store_true", help="inserta de verdad en el CRM")
    r.add_argument("--pdf", action="store_true", help="generar el informe PDF de preventa por lead en out/<lote>/pdf/")
    r.add_argument("--pdf-lang", choices=["en", "es"], help="idioma del PDF (default PDF_LANG o en)")
    r.add_argument("--ignore-seen", action="store_true",
                   help="no saltar edificios ya procesados en corridas anteriores (memoria seen_leads)")
    r.add_argument("--retry-needs-phone", action="store_true",
                   help="reintentar los que quedaron sin teléfono en corridas anteriores")
    r.add_argument("-v", "--verbose", action="store_true")

    i = sub.add_parser("import", help="enviar un crm_payload.json ya generado")
    i.add_argument("rest", nargs=argparse.REMAINDER)

    ds = sub.add_parser("dbpr-sync", help="descargar padrón de condominios + licencias CAM del DBPR")
    ds.add_argument("--groups", help="MD,PB,NF,CE,CW (default DBPR_GROUPS o MD,PB)")
    ds.add_argument("--no-cam", action="store_true", help="no descargar el padrón de licencias CAM")
    pp = sub.add_parser("pdf", help="informes PDF de preventa desde out/<lote>/leads.jsonl")
    pp.add_argument("leads", help="out/<lote>/leads.jsonl")
    pp.add_argument("--out", help="carpeta destino (default: <lote>/pdf)")
    pp.add_argument("--top", type=int, help="solo los N de mayor lead_score")
    pp.add_argument("--min-score", type=int, default=0)
    pp.add_argument("--lang", choices=["en", "es"], help="idioma (default PDF_LANG o en)")
    pf = sub.add_parser("portfolios", help="ranking de administradoras por portafolio + dossiers PDF")
    pf.add_argument("--top", type=int, default=15)
    pf.add_argument("--depth", type=int, default=10, help="edificios por firma consultados en vivo (catastro/municipio)")
    pf.add_argument("--offline", action="store_true", help="solo el padrón DBPR, sin consultas en vivo")
    pf.add_argument("--pdf", action="store_true", help="dossier ejecutivo de 2 páginas por firma")
    pf.add_argument("--lang", choices=["en", "es"], help="idioma del PDF (default PDF_LANG o en)")
    pf.add_argument("--out", help="carpeta base (default OUT_DIR)")
    ep = sub.add_parser("enrich-phones", help="reintentar los teléfonos de un needs_phone.csv y armar el lote")
    ep.add_argument("csv", help="out/<lote>/needs_phone.csv")
    ep_mode = ep.add_mutually_exclusive_group()
    ep_mode.add_argument("--dry-run", action="store_true", help="enviar al CRM con dry_run=true")
    ep_mode.add_argument("--commit", action="store_true", help="importar al CRM")
    ep.add_argument("--pdf", action="store_true")
    ep.add_argument("--pdf-lang", choices=["en", "es"])
    ep.add_argument("--no-ai", action="store_true")
    pf2 = sub.add_parser("preflight", help="diagnóstico de claves, perfil, padrones y conectividad (OK/PENDIENTE/ERROR)")
    pf2.add_argument("--offline", action="store_true", help="solo configuración, sin probar la red")
    ea = sub.add_parser("export-ads", help="audiencia B2B (Meta / Google / LinkedIn) en CSV desde un lote o el padrón")
    ea.add_argument("source", nargs="?", help="out/<lote>/leads.jsonl (sin argumento: padrón DBPR de la zona)")
    ea.add_argument("--out", help="archivo CSV (default: junto al lote o out/ads/ads_audience.csv)")
    ea.add_argument("--min-units", type=int, help="padrón: mínimo de unidades (default MIN_UNITS)")
    ea.add_argument("--include-npi", action=argparse.BooleanOptionalAction, default=True,
                    help="padrón: clínicas/hospitales del NPI Registry de la zona (default sí)")
    ea.add_argument("--include-lodging", action=argparse.BooleanOptionalAction, default=True,
                    help="padrón: hoteles/edificios de apartamentos con licencia DBPR y teléfono (default sí)")
    ea.add_argument("--include-firms", action=argparse.BooleanOptionalAction, default=True,
                    help="padrón: administradoras y operadores comerciales con su teléfono y sitio (default sí)")
    dy = sub.add_parser("daily", help="rutina diaria: Pool del CRM → corrida → PDFs + outreach → CRM → Slack")
    dy.add_argument("--target-pool", type=int, default=None,
                    help="leads disponibles que debe tener el Pool (default: pool_target del CRM, si no 40)")
    dy.add_argument("--fallback-limit", type=int, default=10,
                    help="tamaño del lote cuando el CRM no informa cuántos quedan y el Pool no está bajo")
    dy.add_argument("--limit", type=int, help="forzar el tamaño del lote (no consulta el Pool)")
    dy.add_argument("--strategy", choices=["onsite", "pain", "portfolio"], default="onsite")
    dy.add_argument("--seed", choices=["dbpr", "pa", "npi"], help="fuente (default: places si hay clave, si no dbpr)")
    dy.add_argument("--commit", action="store_true", help="importar los aceptados al CRM (si no, dry-run)")
    dy.add_argument("--no-slack", action="store_true")
    dy.add_argument("--no-mix", action="store_true", help="una sola fuente en vez del mix de verticales del perfil")
    dy.add_argument("--no-ai", action="store_true")
    fbp = sub.add_parser("feedback", help="leer /api/public/lead-outcomes del CRM: teléfonos malos, administradoras, conversión")
    fbp.add_argument("--since", help="ISO 8601; por defecto desde la última lectura")
    fbp.add_argument("--metrics", action="store_true", help="solo mostrar la conversión guardada (sin red)")
    sb = sub.add_parser("sunbiz-sync", help="indexar el Corporate Data File de Sunbiz dejado en data/sunbiz/")
    sb.add_argument("--dir", help="carpeta con los archivos (default data/sunbiz/)")
    sb.add_argument("--all", action="store_true", help="indexar todas las entidades, no solo asociaciones")
    rt = sub.add_parser("route", help="ruta de inspecciones optimizada por zonas")
    rt.add_argument("input", help="leads.jsonl del lote o export del CRM (.csv/.json) con address e inspection_at")
    rt.add_argument("--date", help="día (YYYY-MM-DD, default hoy en Miami)")
    rt.add_argument("--inspectors", default=None, help="nombres separados por coma (default INSPECTORS)")
    rt.add_argument("--start", default=None, help="'lat,lng' de salida (default ROUTE_START o centro de las visitas)")
    rt.add_argument("--out", help="carpeta destino (default: junto al input)")
    cl = sub.add_parser("cam-load", help="cargar lic38cam.csv ya descargado")
    cl.add_argument("file")
    dl = sub.add_parser("dbpr-load", help="cargar CSVs del DBPR ya descargados")
    dl.add_argument("files", nargs="+")

    args = ap.parse_args(argv)
    from . import tenant
    prof = tenant.use(args.tenant or (None if os.getenv("TENANT") or args.cmd != "daily" else "mb_softwash"))
    if prof:
        logger.info("Perfil de empresa: {}", prof.get("company", {}).get("name") or args.tenant)
    if args.cmd == "preflight":
        from . import preflight
        checks = preflight.run(network=not args.offline)
        print(preflight.table(checks))
        return 1 if any(c.status == preflight.ERROR for c in checks) else 0
    if args.cmd == "export-ads":
        from pathlib import Path

        from . import ads_export
        if args.source:
            src = Path(args.source)
            rows = ads_export.from_records(json.loads(l) for l in src.read_text(encoding="utf-8").splitlines() if l.strip())
            out = Path(args.out) if args.out else src.parent / "ads_audience.csv"
        else:
            rows = ads_export.from_registry(min_units=args.min_units, include_npi=args.include_npi,
                                            include_lodging=args.include_lodging, include_firms=args.include_firms)
            out = Path(args.out) if args.out else Path(settings.out_dir) / "ads" / "ads_audience.csv"
        stats = ads_export.write_csv(rows, out)
        print(json.dumps({"file": str(out), **stats}, ensure_ascii=False, indent=2))
        return 0
    if args.cmd == "enrich-phones":
        from .collectors import needs_phone
        rows = needs_phone.read(args.csv)
        if not settings.google_places_api_key and not search.provider():
            logger.warning("Sin GOOGLE_PLACES_API_KEY ni búsqueda externa: solo la cascada gratuita (OSM, Lodging, web)")
        out = pipeline.run(strategy=needs_phone.main_strategy(rows), limit=len(rows), sources=["needs_phone"],
                           seed=args.csv, use_ai=not args.no_ai, send=args.dry_run or args.commit, commit=args.commit,
                           max_candidates=len(rows), make_pdfs=args.pdf, pdf_lang=args.pdf_lang,
                           retry_needs_phone=True)
        print(f"Lote: {out}/crm_payload.json")
        return 0
    if args.cmd == "daily":
        from . import daily
        s = daily.run(target_pool=args.target_pool, fallback_limit=args.fallback_limit, limit=args.limit,
                      strategy=args.strategy, sources=[args.seed] if args.seed else None, commit=args.commit,
                      use_ai=not args.no_ai, slack=not args.no_slack, mix=False if args.no_mix else None)
        print(json.dumps(s, ensure_ascii=False, indent=2))
        return 0
    if args.cmd == "portfolios":
        from pathlib import Path

        from . import portfolios
        out = portfolios.run(top=args.top, depth=args.depth, live=not args.offline, pdf=args.pdf, lang=args.lang,
                             out_dir=Path(args.out) if args.out else None)
        portfolios.print_table(json.loads((out / "ranking.json").read_text(encoding="utf-8")))
        print(f"Archivos: {out}/ranking.csv · ranking.json" + (" · pdf/" if args.pdf else ""))
        return 0
    if args.cmd == "import":
        return crm_importer.main(args.rest)
    if args.cmd == "dbpr-sync":
        groups = [g.strip() for g in args.groups.split(",")] if args.groups else None
        print(dbpr.sync(groups))
        print({"lodging": lodging.sync(groups)})
        if not args.no_cam:
            print({"cam_licenses": cam.load_and_reindex(cam.sync)})
        return 0
    if args.cmd == "pdf":
        from pathlib import Path

        from . import pdf_generator
        src = Path(args.leads)
        out = Path(args.out) if args.out else src.parent / "pdf"
        files = pdf_generator.generate_batch(pdf_generator.load_jsonl(src), out, top=args.top, min_score=args.min_score,
                                              lang=args.lang)
        from . import inspector_sheet
        recs = pdf_generator.load_jsonl(src)
        if args.top:
            recs = sorted(recs, key=lambda r: -(r.get("lead_score") or 0))[:args.top]
        sheets = inspector_sheet.generate_batch([r for r in recs if (r.get("lead_score") or 0) >= args.min_score],
                                                out.parent / "campo")
        print(f"{len(files)} PDF en {out}/ · {len(sheets)} hojas de campo en {out.parent / 'campo'}/")
        return 0
    if args.cmd == "feedback":
        from . import feedback
        if not args.metrics:
            rep = feedback.sync(since=args.since)
            print(feedback.report_text(rep))
        print(json.dumps({"conversion": feedback.conversion(),
                          "mix_weights": feedback.weighted_mix(settings.daily_mix or [])[1]}, ensure_ascii=False, indent=2))
        return 0
    if args.cmd == "sunbiz-sync":
        from pathlib import Path

        from . import sunbiz
        folder = Path(args.dir) if args.dir else sunbiz.data_dir()
        stats = sunbiz.sync(folder, everything=args.all)
        if not stats["files"]:
            print(f"No hay archivos en {folder}: descargá el Corporate Data File de Sunbiz (trimestral o diario) "
                  "y dejalo ahí (.txt/.dat/.zip, o CSV con encabezados).")
            return 1
        print(json.dumps(stats, indent=2))
        return 0
    if args.cmd == "route":
        return _route(args)
    if args.cmd == "cam-load":
        print({"cam_licenses": cam.load_and_reindex(lambda c: cam.load_csv(args.file, c))})
        return 0
    if args.cmd == "dbpr-load":
        conn = dbpr.connect()
        for f in args.files:
            print(f, dbpr.load_csv(f, conn))
        return 0
    sources = args.source
    seed_file = args.seed
    if args.seed and args.seed.lower() in ("dbpr", "npi", "pa"):
        sources, seed_file = (sources or []) + [args.seed.lower()], None
    if args.min_units is not None or args.render or args.vertical:
        import dataclasses

        from . import config

        changes = {}
        if args.min_units is not None:
            changes["min_units"] = args.min_units
        if args.render:
            changes["render_js"] = args.render
        if args.vertical:
            changes["seed_vertical"] = args.vertical
        config.settings = dataclasses.replace(config.settings, **changes)
        for mod in ("render", "web_extract", "pipeline", "collectors.dbpr_seed"):
            setattr(__import__(f"lead_engine.{mod}", fromlist=["settings"]), "settings", config.settings)
    logger.remove()
    logger.add(sys.stderr, level="DEBUG" if args.verbose else "INFO")
    pipeline.run(
        strategy=args.strategy, limit=args.limit, sources=sources, seed=seed_file,
        use_ai=not args.no_ai, use_apollo=not args.no_apollo, precheck=not args.no_precheck,
        send=args.dry_run or args.commit, commit=args.commit, max_candidates=args.max_candidates,
        use_search=not args.no_search, use_geo=not args.no_geo, use_dbpr=not args.no_dbpr,
        check_mx=False if args.no_mx else None, make_pdfs=args.pdf, pdf_lang=args.pdf_lang,
        ignore_seen=args.ignore_seen, retry_needs_phone=args.retry_needs_phone,
    )
    return 0


def _route(args) -> int:
    import json
    import os
    from datetime import date
    from pathlib import Path

    from . import geocode, route_optimizer as ro

    stops = ro.load(args.input)
    names = [x.strip() for x in (args.inspectors or os.getenv("INSPECTORS", "Inspector 1")).split(",") if x.strip()]
    day = date.fromisoformat(args.date) if args.date else None
    raw_start = args.start or os.getenv("ROUTE_START")
    if raw_start:
        lat, lng = (float(x) for x in raw_start.split(","))
        start = (lat, lng)
    else:
        for s in stops:
            if s.lat is None:
                hit = geocode.geocode(s.address)
                if hit:
                    s.lat, s.lng = hit
        pts = [(s.lat, s.lng) for s in stops if s.lat is not None]
        start = (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts)) if pts else (25.7617, -80.1918)
    plan = ro.optimize(stops, inspectors=names, start=start, day=day, geocoder=geocode.geocode)
    out = Path(args.out) if args.out else Path(args.input).parent
    out.mkdir(parents=True, exist_ok=True)
    stem = f"ruta_{plan.day.isoformat()}"
    (out / f"{stem}.md").write_text(ro.to_markdown(plan), encoding="utf-8")
    ro.to_csv(plan, out / f"{stem}.csv")
    sheets = ro.field_sheets(plan, out, stem)
    (out / f"{stem}.json").write_text(json.dumps(ro.to_json(plan), ensure_ascii=False, indent=2), encoding="utf-8")
    print(ro.to_markdown(plan))
    print(f"Archivos: {out}/{stem}.md · .csv · .json" + (f" · {len(sheets)} hoja(s) de campo" if sheets else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
