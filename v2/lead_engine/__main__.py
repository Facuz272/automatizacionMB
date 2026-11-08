"""CLI.

  python -m lead_engine run --strategy onsite --limit 20 --dry-run
  python -m lead_engine run --strategy portfolio --limit 50 --commit
  python -m lead_engine run --source csv --seed dbpr_condos.csv --strategy onsite --limit 20 --dry-run
  python -m lead_engine import out/<lote>/crm_payload.json [--commit]
  python -m lead_engine run --seed dbpr --strategy onsite --limit 20 --dry-run   # semilla desde el padrón DBPR
  python -m lead_engine dbpr-sync [--groups MD,PB]         # padrón de condominios + licencias CAM (DBPR)
  python -m lead_engine dbpr-load Condo_MD.csv [...]       # o cargar CSVs ya descargados
"""

from __future__ import annotations

import argparse
import sys

from loguru import logger

from . import cam, crm_importer, dbpr, lodging, pipeline


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="lead_engine", description="MB Lead Engine v2")
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
    r.add_argument("-v", "--verbose", action="store_true")

    i = sub.add_parser("import", help="enviar un crm_payload.json ya generado")
    i.add_argument("rest", nargs=argparse.REMAINDER)

    ds = sub.add_parser("dbpr-sync", help="descargar padrón de condominios + licencias CAM del DBPR")
    ds.add_argument("--groups", help="MD,PB,NF,CE,CW (default DBPR_GROUPS o MD,PB)")
    ds.add_argument("--no-cam", action="store_true", help="no descargar el padrón de licencias CAM")
    cl = sub.add_parser("cam-load", help="cargar lic38cam.csv ya descargado")
    cl.add_argument("file")
    dl = sub.add_parser("dbpr-load", help="cargar CSVs del DBPR ya descargados")
    dl.add_argument("files", nargs="+")

    args = ap.parse_args(argv)
    if args.cmd == "import":
        return crm_importer.main(args.rest)
    if args.cmd == "dbpr-sync":
        groups = [g.strip() for g in args.groups.split(",")] if args.groups else None
        print(dbpr.sync(groups))
        print({"lodging": lodging.sync(groups)})
        if not args.no_cam:
            print({"cam_licenses": cam.load_and_reindex(cam.sync)})
        return 0
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
        check_mx=False if args.no_mx else None,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
