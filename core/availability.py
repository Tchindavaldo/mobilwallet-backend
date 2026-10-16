"""Disponibilité des opérateurs par agrégateur (neutre).

Chaque agrégateur qui publie l'état de ses opérateurs expose
`aggregators.<nom>.availability.get_availability()` ->
[{network, provider, operations: {deposit|payout|refund: OPERATIONAL|DELAYED|CLOSED}}].

- `list_availability` : pour l'endpoint dédié GET /availability.
- `guard` : appelé par /pay et /payout ; refuse (503) une opération dont
  l'opérateur est CLOSED, 400 si l'opération est NOT_ENABLED (non activée sur
  le compte marchand) ; DELAYED passe (file d'attente) avec avertissement.
  FAIT publié par l'agrégateur, pas un
  verdict de paiement. Agrégateur sans ce module ou injoignable -> on laisse passer.
"""

import importlib
import logging

from fastapi import HTTPException

log = logging.getLogger("ai_browser2")

# Doc pawaPay : CLOSED = demandes rejetées (dépôts, payouts, refunds) ;
# DELAYED = payouts/refunds mis en file d'attente, exécutés plus tard (pas un refus).
_BLOCKING = ("CLOSED",)


def delayed_message(network: str) -> str:
    """Message UTILISATEUR d'un payout accepté alors que l'opérateur est DELAYED."""
    other = "Orange Money" if (network or "").upper().startswith("MTN") else "MTN"
    return ("Virement accepté mais mis en file d'attente : l'opérateur a un problème "
            "temporaire. Si vous le laissez, il restera en attente et sera envoyé "
            "automatiquement dès le rétablissement de l'opérateur, ce qui peut prendre "
            f"du temps. Sinon, vous pouvez l'annuler et réessayer sur un autre réseau ({other}).")


DELAYED_DEV_MESSAGE = ("Opérateur DELAYED chez l'agrégateur : payout mis en file (ENQUEUED). "
                       "Annulable tant qu'il est en file via POST /payout/{ref}/cancel.")


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


_NOT_ENABLED_MSG = {
    "deposit": "Les paiements ne sont pas encore activés sur ce réseau. Utilisez un autre réseau.",
    "payout": "Les retraits ne sont pas encore activés sur ce réseau.",
    "refund": "Les remboursements ne sont pas encore activés sur ce réseau.",
}


async def guard(aggregator: str, network: str, operation: str) -> str | None:
    """Lève 400 operation_not_enabled si l'opération n'est pas activée sur le
    compte marchand, 503 operator_unavailable si l'opérateur est CLOSED.
    Retourne le statut (ex. DELAYED pour prévenir le client), ou None si inconnu."""
    status = await operator_status(aggregator, network, operation)
    if status == "NOT_ENABLED":
        raise HTTPException(400, {
            "code": "operation_not_enabled",
            "message": _NOT_ENABLED_MSG.get(operation, "Opération non activée sur ce réseau."),
            "dev_message": (f"'{operation}' n'est pas activé sur le compte marchand "
                            f"{aggregator} pour {network} : à demander à l'agrégateur."),
            "aggregator": aggregator, "network": network, "operation": operation,
            "operator_status": status,
        })
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
    return status
