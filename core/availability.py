"""Disponibilité des opérateurs par agrégateur (neutre).

Chaque agrégateur qui publie l'état de ses opérateurs expose
`aggregators.<nom>.availability.get_availability()` ->
[{network, provider, operations: {deposit|payout|refund: OPERATIONAL|DELAYED|CLOSED}}].

- `list_availability` : pour l'endpoint dédié GET /availability.
- `guard` : appelé par /pay et /payout ; refuse (503) une opération dont
  l'opérateur n'est pas OPERATIONAL. FAIT publié par l'agrégateur, pas un
  verdict de paiement. Agrégateur sans ce module ou injoignable -> on laisse passer.
"""

import importlib
import logging

from fastapi import HTTPException

log = logging.getLogger("ai_browser2")

_BLOCKING = ("CLOSED", "DELAYED")


def _module(aggregator: str):
    try:
        return importlib.import_module(f"aggregators.{aggregator}.availability")
    except ModuleNotFoundError:
        return None


async def list_availability(aggregator: str) -> list | None:
    """État des opérateurs de l'agrégateur ; [] s'il ne publie rien, None si injoignable."""
    mod = _module(aggregator)
    if mod is None:
        return []
    return await mod.get_availability()


async def operator_status(aggregator: str, network: str, operation: str) -> str | None:
    """Statut de l'opérateur `network` pour `operation`, ou None si inconnu."""
    rows = await list_availability(aggregator)
    for r in rows or []:
        if (r.get("network") or "").lower() == (network or "").lower():
            return r["operations"].get(operation)
    return None


async def guard(aggregator: str, network: str, operation: str) -> None:
    """Lève 503 operator_unavailable si l'opérateur est DELAYED ou CLOSED."""
    status = await operator_status(aggregator, network, operation)
    if status in _BLOCKING:
        log.warning("Opérateur %s indisponible (%s, %s) : %s refusé",
                    network, aggregator, status, operation)
        mod = _module(aggregator)
        msg = (mod.unavailable_message(network) if hasattr(mod, "unavailable_message")
               else f"L'opérateur {network} est momentanément indisponible. "
                    f"Réessayez plus tard ou utilisez un autre réseau.")
        raise HTTPException(503, {
            "code": "operator_unavailable",
            "message": msg,
            "aggregator": aggregator, "network": network,
            "operation": operation, "operator_status": status,
        })
