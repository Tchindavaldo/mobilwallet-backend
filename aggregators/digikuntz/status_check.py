"""Vérification à la demande du statut d'un paiement DigiKUNTZ : UN appel
GET {base}/transaction, sans polling. Utilisé par core/status_check.py."""

from . import status_poll


async def check_deposit_status(provider_id: str) -> dict | None:
    """Retourne {status, raw, message, terminal} ou None si DigiKUNTZ est injoignable.
    Un statut terminal est déposé dans le registre (réveille un suivi en cours)."""
    res = await status_poll.fetch_status(provider_id)
    if res is None:
        return None
    status = res["internal"]
    terminal = status in status_poll._TERMINAL
    if terminal:
        status_poll.registry.deliver(
            provider_id, {"status": status, "raw": res["raw"], "data": res.get("data")})
    return {"status": status, "raw": res["raw"], "terminal": terminal,
            "message": f"Statut DigiKUNTZ : {res['raw']}"}
