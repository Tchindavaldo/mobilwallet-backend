"""Incidents publiés sur la page de statut publique pawaPay
(status.pawapay.io, API Statuspage sans clé), cache 60 s. Croisé avec
GET /v2/availability par availability.py : l'API marchand peut rester
OPERATIONAL alors qu'un incident est déclaré sur le dashboard."""

import logging
import time
from datetime import datetime, timedelta, timezone

import httpx

from aggregators.pawapay.operations import network_of

log = logging.getLogger("ai_browser2")

_URL = "https://status.pawapay.io/api/v2/summary.json"
_INCIDENTS_URL = "https://status.pawapay.io/api/v2/incidents.json"
_TTL = 60.0
_cache: dict = {"at": 0.0, "data": None}
_inc_cache: dict = {"at": 0.0, "data": None}

# Après la résolution d'un incident, l'opération reste bloquée 1 h : constaté,
# pawaPay remet le composant à operational avant que l'USSD reparte vraiment.
COOLDOWN_S = 3600

# Groupe de composants par pays (code pawaPay -> nom du groupe sur la page).
_COUNTRY_GROUP = {"CMR": "Cameroon"}
# Préfixe du composant -> code provider ; suffixe -> operationType.
_PROVIDERS = {"MTN": "MTN_MOMO_CMR", "Orange": "ORANGE_CMR"}
_OPS = {"Web Deposit": "DEPOSIT", "Payout": "PAYOUT"}

# Tout incident déclaré bloque : composant dégradé ou incident ouvert (même si
# le composant est revenu à operational) -> CLOSED (/pay et /payout en 503).
STATUS_MAP = {"degraded_performance": "CLOSED", "partial_outage": "CLOSED",
              "major_outage": "CLOSED"}


async def _fetch(url: str, cache: dict) -> dict | None:
    if cache["data"] is not None and time.monotonic() - cache["at"] < _TTL:
        return cache["data"]
    try:
        async with httpx.AsyncClient(timeout=8) as http:
            r = await http.get(url)
            r.raise_for_status()
            data = r.json()
    except Exception as exc:  # page injoignable : on s'en tient à l'API marchand
        log.warning("Page de statut pawaPay injoignable (%s) : %s", url, exc)
        return None
    cache.update(at=time.monotonic(), data=data)
    return data


async def _summary() -> dict | None:
    return await _fetch(_URL, _cache)


async def _cooling_down() -> dict:
    """{component_id: (nom incident, fin du délai ISO)} pour les incidents
    résolus depuis moins de COOLDOWN_S."""
    data = await _fetch(_INCIDENTS_URL, _inc_cache)
    now = datetime.now(timezone.utc)
    out = {}
    for inc in (data or {}).get("incidents", []):
        resolved = inc.get("resolved_at")
        if inc.get("status") not in ("resolved", "postmortem") or not resolved:
            continue
        until = datetime.fromisoformat(resolved.replace("Z", "+00:00")) + timedelta(seconds=COOLDOWN_S)
        if until <= now:
            continue
        for comp in inc.get("components", []):
            prev = out.get(comp.get("id"))
            if not prev or prev[1] < until.isoformat():
                out[comp.get("id")] = (inc.get("name"), until.isoformat())
    return out


async def incidents(country: str) -> dict:
    """{(provider, operationType): {status, component_status, incident[,
    cooldown_until]}} pour les composants dégradés du pays, ou dont l'incident
    est résolu depuis moins de COOLDOWN_S ; {} si rien ou page injoignable."""
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
    cooling = await _cooling_down()
    out = {}
    for c in data.get("components", []):
        if c.get("group_id") not in groups:
            continue
        cool = cooling.get(c.get("id")) if c.get("id") not in names else None
        if c.get("status") not in STATUS_MAP and c.get("id") not in names and not cool:
            continue
        prov, _, op = (c.get("name") or "").partition(" :: ")
        key = (_PROVIDERS.get(prov.strip()), _OPS.get(op.strip()))
        if None in key:
            continue
        out[key] = {"status": STATUS_MAP.get(c["status"], "CLOSED"), "component_status": c["status"],
                    "incident": names.get(c.get("id")) or (cool and cool[0])}
        if cool and c.get("status") not in STATUS_MAP and c.get("id") not in names:
            out[key]["cooldown_until"] = cool[1]
    return out


async def page_status(country: str) -> dict | None:
    """État publié sur la page de statut pour le pays : composants (réseau,
    opération, statut, mise à jour) et incidents non résolus. None si injoignable."""
    data = await _summary()
    if data is None:
        return None
    group_prefix = _COUNTRY_GROUP.get(country, "")
    groups = {c["id"] for c in data.get("components", [])
              if c.get("group") and c.get("name", "").startswith(group_prefix)}
    components = []
    for c in data.get("components", []):
        if c.get("group_id") not in groups:
            continue
        prov, _, op = (c.get("name") or "").partition(" :: ")
        components.append({"network": network_of(_PROVIDERS.get(prov.strip(), prov.strip())),
                           "operation": op.strip(), "status": c.get("status"),
                           "updated_at": c.get("updated_at")})
    ids = {c["id"] for c in data.get("components", []) if c.get("group_id") in groups}
    incidents_open = [
        {"name": i.get("name"), "status": i.get("status"), "started_at": i.get("started_at"),
         "updated_at": i.get("updated_at"),
         "last_update": (i.get("incident_updates") or [{}])[0].get("body")}
        for i in data.get("incidents", [])
        if i.get("status") not in ("resolved", "postmortem")
        and any(comp.get("id") in ids for comp in i.get("components", []))]
    return {"source": _URL.replace("/api/v2/summary.json", ""), "country": country,
            "components": components, "incidents": incidents_open}


async def get_page_status() -> dict | None:
    """Point d'entrée neutre (core/availability.status_page) : pays du compte."""
    from aggregators.pawapay import client
    return await page_status(client._pp.country)
