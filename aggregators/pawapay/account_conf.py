"""Configuration du compte marchand pawaPay (GET /v2/active-conf), cache 10 min :
ce qui est ACTIVÉ pour ce compte (par réseau et opération), limites de montant
et code USSD de secours pour valider le PIN. Lu par core/deposit_limits.py,
aggregators/pawapay/availability.py et operations.ussd_code.
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


async def _operations() -> dict | None:
    """{'mtn': {'DEPOSIT': {...}, 'REFUND': {...}}, ...} ; None si injoignable."""
    conf = await _conf()
    if conf is None:
        return None
    out: dict = {}
    for country in conf.get("countries") or []:
        for prov in country.get("providers") or []:
            net = _NETWORK.get(prov.get("provider"))
            if not net:
                continue
            for cur in prov.get("currencies") or []:
                for op, cfg in (cur.get("operationTypes") or {}).items():
                    if cfg:  # NAME_LOOKUP: {} = non configuré
                        out.setdefault(net, {})[op] = cfg
    return out


async def enabled_operations() -> dict | None:
    """{'mtn': {'DEPOSIT', 'REFUND'}, ...} ; None si la config est injoignable."""
    ops = await _operations()
    return None if ops is None else {n: set(o) for n, o in ops.items()}


async def amount_limits(operation: str = "DEPOSIT") -> dict:
    """{'mtn': {'min': 1, 'max': 1000000}, ...} ; {} si injoignable."""
    out: dict = {}
    for net, ops in (await _operations() or {}).items():
        op = ops.get(operation) or {}
        if op.get("minAmount") is not None:
            out[net] = {"min": int(float(op["minAmount"])),
                        "max": int(float(op.get("maxAmount") or 0)) or None}
    return out


async def ussd_code(network: str, operation: str = "DEPOSIT") -> str | None:
    """Code USSD à composer si la demande de PIN n'arrive pas (ex. *126#)."""
    ops = await _operations() or {}
    cfg = (ops.get(network) or {}).get(operation) or {}
    for ch in (cfg.get("pinPromptInstructions") or {}).get("channels") or []:
        code = (ch.get("variables") or {}).get("shortCode")
        if code:
            return code
    return None
