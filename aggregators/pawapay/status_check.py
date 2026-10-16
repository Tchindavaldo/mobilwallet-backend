"""Vérification à la demande du statut d'un dépôt pawaPay : UN appel
GET /v2/deposits/{id}, sans polling. Utilisé par core/status_check.py."""

from . import client


async def check_deposit_status(provider_id: str) -> dict | None:
    """Retourne {status, raw, message, terminal} ou None si pawaPay est injoignable.
    Un statut terminal est déposé dans le registre (réveille un suivi en cours)."""
    res = await client.fetch_status("deposits", provider_id)
    if res is None:
        return None
    status = res["internal"]
    terminal = status in client.TERMINAL
    if terminal:
        client.registry.deliver(provider_id, {"status": status, "raw": res["raw"],
                                              "failure": res["failure"]})
    detail = client.failure_message(res["failure"]) if status in ("failed", "cancelled") else ""
    return {"status": status, "raw": res["raw"], "terminal": terminal,
            "message": detail or f"Statut pawaPay : {res['raw']}"}
