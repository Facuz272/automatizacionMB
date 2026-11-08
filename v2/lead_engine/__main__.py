"""CLI.

  python -m lead_engine run --strategy onsite --limit 20 --dry-run
  python -m lead_engine run --strategy portfolio --limit 50 --commit
  python -m lead_engine run --source csv --seed dbpr_condos.csv --strategy onsite --limit 20 --dry-run
  python -m lead_engine import out/<lote>/crm_payload.json [--commit]
"""

from __future__ import annotations

import argparse
import sys

from loguru import logger

from . import crm_importer, pipeline


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
    mode = r.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true",
                      help="envía al CRM con dry_run=true (no escribe) y guarda la respuesta")
    mode.add_argument("--commit", action="store_true", help="inserta de verdad en el CRM")
    r.add_argument("-v", "--verbose", action="store_true")

    i = sub.add_parser("import", help="enviar un crm_payload.json ya generado")
    i.add_argument("rest", nargs=argparse.REMAINDER)

    args = ap.parse_args(argv)
    if args.cmd == "import":
        return crm_importer.main(args.rest)

    logger.remove()
    logger.add(sys.stderr, level="DEBUG" if args.verbose else "INFO")
    pipeline.run(
        strategy=args.strategy, limit=args.limit, sources=args.source or ["places"], seed=args.seed,
        use_ai=not args.no_ai, use_apollo=not args.no_apollo, precheck=not args.no_precheck,
        send=args.dry_run or args.commit, commit=args.commit, max_candidates=args.max_candidates,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
