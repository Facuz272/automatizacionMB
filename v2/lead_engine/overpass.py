"""Overpass API with mirror failover (shared by geo.fetch_profile and the OSM collector).

Endpoints: OVERPASS_URL (primary) then OVERPASS_MIRRORS (comma separated; default the kumi.systems
and lz4.overpass-api.de mirrors). A connection reset, timeout, 429 or 5xx moves to the next endpoint
right away (short connect timeout); the endpoint that answered is used first for the rest of the run.
After MAX_ROUNDS consecutive calls where every endpoint failed, Overpass is switched off for the run
so a dead OSM never slows a batch — the cadastre/DBPR inference fills the gaps.
"""

from __future__ import annotations

import os
from typing import Any

import requests
from loguru import logger

from . import costs
from .config import settings

DEFAULT_MIRRORS = ("https://overpass.kumi.systems/api/interpreter", "https://lz4.overpass-api.de/api/interpreter")
CONNECT_TIMEOUT = 6.0
MIRROR_READ_TIMEOUT = 15.0       # a mirror that accepts the connection and then hangs must not stall the run
MAX_ROUNDS = 2

_state: dict[str, Any] = {"preferred": None, "failed_rounds": 0, "disabled": False}


class OverpassUnavailable(RuntimeError):
    pass


def endpoints() -> list[str]:
    raw = os.getenv("OVERPASS_MIRRORS")
    mirrors = [m.strip() for m in raw.split(",") if m.strip()] if raw is not None else list(DEFAULT_MIRRORS)
    out = []
    for u in [_state["preferred"], settings.overpass_url, *mirrors]:
        if u and u not in out:
            out.append(u)
    return out


def reset() -> None:
    _state.update(preferred=None, failed_rounds=0, disabled=False)


def query(data: str, timeout: float = 45) -> list[dict]:
    """Elements of an Overpass QL query; raises OverpassUnavailable when every endpoint failed."""
    if _state["disabled"]:
        raise OverpassUnavailable("Overpass desactivado en esta corrida")
    errors = []
    for i, url in enumerate(endpoints()):
        read = timeout if i == 0 else min(timeout, MIRROR_READ_TIMEOUT)
        try:
            r = requests.post(url, data={"data": data}, headers={"User-Agent": settings.user_agent},
                              timeout=(CONNECT_TIMEOUT, read))
            costs.tracker.add("overpass")
            if r.status_code == 429 or r.status_code >= 500:
                raise requests.HTTPError(f"HTTP {r.status_code}")
            r.raise_for_status()
            elements = r.json().get("elements", [])
        except (requests.RequestException, ValueError) as exc:
            errors.append(f"{url.split('/')[2]}: {exc.__class__.__name__}")
            continue
        if url != _state["preferred"] and errors:
            logger.info("Overpass: usando el mirror {} ({})", url.split("/")[2], "; ".join(errors))
        _state.update(preferred=url, failed_rounds=0)
        return elements
    _state["failed_rounds"] += 1
    if _state["failed_rounds"] >= MAX_ROUNDS:
        _state["disabled"] = True
        logger.warning("Overpass sin respuesta en {} endpoints; se desactiva para esta corrida", len(errors))
    raise OverpassUnavailable("; ".join(errors))
