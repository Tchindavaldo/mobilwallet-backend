"""Disponibilité des opérateurs chez pawaPay (GET /v2/availability), mise en
cache 60 s. Utilisé par core/availability.py (endpoint dédié + garde /pay, /payout).

Statuts pawaPay : OPERATIONAL (normal), DELAYED (opérations mises en file, très
lentes), CLOSED (refusées). Ce sont des FAITS publiés par pawaPay.
Croisé avec la config du compte (account_conf) : une opération non activée sur
ce compte est NOT_ENABLED, même si l'opérateur fonctionne chez pawaPay.
La page de statut publique (status_page) n'intervient PAS ici : constaté, un
paiement peut passer malgré un incident affiché ; seul /v2/availability fait foi."""

import time

from aggregators.pawapay import account_conf, client
from aggregators.pawapay.operations import network_of

_TTL = 60.0
_cache: dict = {"at": 0.0, "data": None}

# Opération MobileWallet -> operationType pawaPay.
_OPS = {"deposit": "DEPOSIT", "payout": "PAYOUT", "refund": "REFUND"}
_CONF_NET = {"MTN_MOMO_CMR": "mtn", "ORANGE_CMR": "orange"}


def _op_statuses(entry: dict) -> dict:
    """{DEPOSIT: status, ...} ; accepte la forme liste (v2) ou dict."""
    ops = entry.get("operationTypes") or {}
    if isinstance(ops, dict):
        return {k: (v.get("status") if isinstance(v, dict) else v) for k, v in ops.items()}
    return {o.get("operationType"): o.get("status") for o in ops if isinstance(o, dict)}


async def _raw() -> list | None:
    if _cache["data"] is not None and time.monotonic() - _cache["at"] < _TTL:
        return _cache["data"]
    data = await client.fetch_availability()
    if isinstance(data, list):
        _cache.update(at=time.monotonic(), data=data)
        return data
    return None


async def get_availability() -> list | None:
    """[{network, provider, operations: {deposit, payout, refund}}] ; None si
    pawaPay est injoignable."""
    data = await _raw()
    if data is None:
        return None
    enabled = await account_conf.enabled_operations()  # None = config injoignable
    out = []
    for country in data:
        for p in country.get("providers") or []:
            code = p.get("provider", "")
            st = _op_statuses(p)
            if enabled is not None:
                mine = enabled.get(_CONF_NET.get(code, ""), set())
                st = {pp: ("NOT_ENABLED" if pp not in mine else st.get(pp))
                      for pp in _OPS.values() if pp not in mine or st.get(pp)}
            out.append({
                "network": network_of(code),
                "provider": code,
                "operations": {op: st.get(pp) for op, pp in _OPS.items() if pp in st},
            })
    return out


def unavailable_message(network: str) -> str:
    """Même message que le refus pawaPay PROVIDER_TEMPORARILY_UNAVAILABLE :
    indique au client de réessayer plus tard ou de passer par l'autre réseau."""
    return client.failure_message({"failureCode": "PROVIDER_TEMPORARILY_UNAVAILABLE"}, network)
