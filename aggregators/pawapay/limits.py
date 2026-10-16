"""Limites de montant publiées par pawaPay pour chaque opérateur (GET
/v2/active-conf) : {network: {"min": n, "max": n}} pour une opération.
Cache 10 min (la config du compte bouge rarement). Lu par core/deposit_limits.py.
"""

import time

from aggregators.pawapay import client

_TTL_S = 600
_cache: dict = {"at": 0.0, "conf": None}
_NETWORK = {"MTN_MOMO_CMR": "mtn", "ORANGE_CMR": "orange"}


async def _conf() -> dict | None:
    if _cache["conf"] is None or time.monotonic() - _cache["at"] > _TTL_S:
        conf = await client.fetch_active_conf()
        if conf:
            _cache.update(at=time.monotonic(), conf=conf)
    return _cache["conf"]


async def amount_limits(operation: str = "DEPOSIT") -> dict:
    """{'mtn': {'min': 1, 'max': 1000000}, 'orange': {...}} ; {} si injoignable."""
    out: dict = {}
    for country in (await _conf() or {}).get("countries") or []:
        for prov in country.get("providers") or []:
            net = _NETWORK.get(prov.get("provider"))
            for cur in prov.get("currencies") or []:
                op = (cur.get("operationTypes") or {}).get(operation) or {}
                if net and op.get("minAmount") is not None:
                    out[net] = {"min": int(float(op["minAmount"])),
                                "max": int(float(op.get("maxAmount") or 0)) or None}
    return out
