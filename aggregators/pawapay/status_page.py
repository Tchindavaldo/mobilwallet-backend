"""Incidents publiés sur la page de statut publique pawaPay
(status.pawapay.io, API Statuspage sans clé), cache 60 s. Croisé avec
GET /v2/availability par availability.py : l'API marchand peut rester
OPERATIONAL alors qu'un incident est déclaré sur le dashboard."""

import logging
import time

import httpx

log = logging.getLogger("ai_browser2")

_URL = "https://status.pawapay.io/api/v2/summary.json"
_TTL = 60.0
_cache: dict = {"at": 0.0, "data": None}

# Groupe de composants par pays (code pawaPay -> nom du groupe sur la page).
_COUNTRY_GROUP = {"CMR": "Cameroon"}
# Préfixe du composant -> code provider ; suffixe -> operationType.
_PROVIDERS = {"MTN": "MTN_MOMO_CMR", "Orange": "ORANGE_CMR"}
_OPS = {"Web Deposit": "DEPOSIT", "Payout": "PAYOUT"}

# Statuts Statuspage -> statut MobileWallet. DEGRADED passe (avertissement),
# CLOSED bloque (/pay et /payout refusés en 503). Composant operational mais
# incident ouvert -> DEGRADED.
STATUS_MAP = {"degraded_performance": "DEGRADED", "partial_outage": "DEGRADED",
              "major_outage": "CLOSED"}


async def _summary() -> dict | None:
    if _cache["data"] is not None and time.monotonic() - _cache["at"] < _TTL:
        return _cache["data"]
    try:
        async with httpx.AsyncClient(timeout=8) as http:
            r = await http.get(_URL)
            r.raise_for_status()
            data = r.json()
    except Exception as exc:  # page injoignable : on s'en tient à l'API marchand
        log.warning("Page de statut pawaPay injoignable : %s", exc)
        return None
    _cache.update(at=time.monotonic(), data=data)
    return data


async def incidents(country: str) -> dict:
    """{(provider, operationType): {status, component_status, incident}} pour
    les composants dégradés du pays ; {} si rien ou page injoignable."""
    data = await _summary()
    group_prefix = _COUNTRY_GROUP.get(country)
    if not data or not group_prefix:
        return {}
    groups = {c["id"] for c in data.get("components", [])
              if c.get("group") and c.get("name", "").startswith(group_prefix)}
    # Incident ouvert (non resolved) : il compte même si pawaPay a déjà remis
    # le composant à operational.
    names = {}
    for inc in data.get("incidents", []):
        if inc.get("status") in ("resolved", "postmortem"):
            continue
        for comp in inc.get("components", []):
            names[comp.get("id")] = inc.get("name")
    out = {}
    for c in data.get("components", []):
        if c.get("group_id") not in groups:
            continue
        if c.get("status") not in STATUS_MAP and c.get("id") not in names:
            continue
        prov, _, op = (c.get("name") or "").partition(" :: ")
        key = (_PROVIDERS.get(prov.strip()), _OPS.get(op.strip()))
        if None in key:
            continue
        out[key] = {"status": STATUS_MAP.get(c["status"], "DEGRADED"), "component_status": c["status"],
                    "incident": names.get(c.get("id"))}
    return out
