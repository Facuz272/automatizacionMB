"""CLI.

  python -m lead_engine run --strategy onsite --limit 20 --dry-run
  python -m lead_engine run --strategy portfolio --limit 50 --commit
  python -m lead_engine run --source csv --seed dbpr_condos.csv --strategy onsite --limit 20 --dry-run
  python -m lead_engine import out/<lote>/crm_payload.json [--commit]
  python -m lead_engine dbpr-sync [--groups MD,PB]         # padrón oficial de condominios (DBPR)
  python -m lead_engine dbpr-load Condo_MD.csv [...]       # o cargar CSVs ya descargados
"""

from __future__ import annotations

import argparse
import sys

from loguru import logger

from . import crm_importer, dbpr, pipeline


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="lead_engine", description="MB Lead Engine v2")
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="recolectar, validar, enriquecer y (opcional) enviar al CRM")
    r.add_argument("--strategy", choices=["onsite", "pain", "portfolio"], default="onsite")
    r.add_argument("--limit", type=int, default=20, help="leads enriquecidos a producir")
    r.add_argument("--source", action="append", choices=["places", "osm", "csv"],
                   help="repetible; por defecto places")
    r.add_argument("--seed", help="CSV para --source csv")
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

    ds = sub.add_parser("dbpr-sync", help="descargar y cargar el padrón oficial de condominios del DBPR")
    ds.add_argument("--groups", help="MD,PB,NF,CE,CW (default DBPR_GROUPS o MD,PB)")
    dl = sub.add_parser("dbpr-load", help="cargar CSVs del DBPR ya descargados")
    dl.add_argument("files", nargs="+")

    args = ap.parse_args(argv)
    if args.cmd == "import":
        return crm_importer.main(args.rest)
    if args.cmd == "dbpr-sync":
        groups = [g.strip() for g in args.groups.split(",")] if args.groups else None
        print(dbpr.sync(groups))
        return 0
    if args.cmd == "dbpr-load":
        conn = dbpr.connect()
        for f in args.files:
            print(f, dbpr.load_csv(f, conn))
        return 0
    if args.render:
        import dataclasses

        from . import config

        config.settings = dataclasses.replace(config.settings, render_js=args.render)
        for mod in ("render", "web_extract", "pipeline"):
            setattr(__import__(f"lead_engine.{mod}", fromlist=["settings"]), "settings", config.settings)

    logger.remove()
    logger.add(sys.stderr, level="DEBUG" if args.verbose else "INFO")
    pipeline.run(
        strategy=args.strategy, limit=args.limit, sources=args.source or ["places"], seed=args.seed,
        use_ai=not args.no_ai, use_apollo=not args.no_apollo, precheck=not args.no_precheck,
        send=args.dry_run or args.commit, commit=args.commit, max_candidates=args.max_candidates,
        use_search=not args.no_search, use_geo=not args.no_geo, use_dbpr=not args.no_dbpr,
        check_mx=False if args.no_mx else None,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
