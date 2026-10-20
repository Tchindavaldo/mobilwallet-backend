"""État publié sur la page de statut publique pawaPay (status.pawapay.io, API
Statuspage sans clé), cache 60 s. INFORMATIF seulement (GET
/availability/status-page) : ne bloque aucun paiement, seul /v2/availability
(availability.py) fait foi."""

import logging
import time

import httpx

from aggregators.pawapay.operations import network_of

log = logging.getLogger("ai_browser2")

_URL = "https://status.pawapay.io/api/v2/summary.json"
_TTL = 60.0
_cache: dict = {"at": 0.0, "data": None}

# Groupe de composants par pays (code pawaPay -> nom du groupe sur la page).
_COUNTRY_GROUP = {"CMR": "Cameroon"}
# Préfixe du composant -> code provider.
_PROVIDERS = {"MTN": "MTN_MOMO_CMR", "Orange": "ORANGE_CMR"}


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
