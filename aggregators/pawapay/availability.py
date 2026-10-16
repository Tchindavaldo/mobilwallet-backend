"""Disponibilité des opérateurs chez pawaPay (GET /v2/availability), mise en
cache 60 s. Utilisé par core/availability.py (endpoint dédié + garde /pay, /payout).

Statuts pawaPay : OPERATIONAL (normal), DELAYED (opérations mises en file, très
lentes), CLOSED (refusées). Ce sont des FAITS publiés par pawaPay."""

import time

from aggregators.pawapay import client

_TTL = 60.0
_cache: dict = {"at": 0.0, "data": None}

# Opération MobileWallet -> operationType pawaPay.
_OPS = {"deposit": "DEPOSIT", "payout": "PAYOUT", "refund": "REFUND"}
# Code provider pawaPay -> réseau canonique MobileWallet.
_NETWORKS = {"MTN_MOMO_CMR": "MTN", "ORANGE_CMR": "Orangemoney"}


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
    out = []
    for country in data:
        for p in country.get("providers") or []:
            code = p.get("provider", "")
            st = _op_statuses(p)
            out.append({
                "network": _NETWORKS.get(code, code),
                "provider": code,
                "operations": {op: st.get(pp) for op, pp in _OPS.items() if pp in st},
            })
    return out
