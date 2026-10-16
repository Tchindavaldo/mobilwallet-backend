"""Vérification à la demande du statut d'un dépôt pawaPay : UN appel
GET /v2/deposits/{id}, sans polling. Utilisé par core/status_check.py.

Page de paiement hébergée (mode "page") : si le client abandonne la page,
pawaPay ne crée jamais le dépôt (NOT_FOUND) et n'envoie aucun callback ; sa doc
dit de le considérer FAILED après 15 min. C'est un FAIT d'expiration publié par
pawaPay (comme les 17 min USSD), pas un verdict déduit."""

from datetime import datetime, timezone

from . import client
from .operations import PAGE_EXPIRY_S, network_of


def _age_s(created_at: str | None) -> float:
    try:
        dt = datetime.fromisoformat((created_at or "").replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - dt).total_seconds()
    except ValueError:
        return 0.0


def _payer(data: dict) -> dict:
    acc = (data.get("payer") or {}).get("accountDetails") or {}
    return {"network": network_of(acc.get("provider", "")), "phone": acc.get("phoneNumber", "")}


async def check_deposit_status(provider_id: str, tx: dict | None = None) -> dict | None:
    """Retourne {status, raw, message, terminal, payer} ou None si pawaPay est
    injoignable. Un statut terminal est déposé dans le registre."""
    res = await client.fetch_status("deposits", provider_id)
    if res is None:
        return None
    tx = tx or {}
    if (res["raw"] == "NOT_FOUND" and tx.get("mode") == "page"
            and _age_s(tx.get("created_at")) >= PAGE_EXPIRY_S):
        return {"status": "failed", "raw": "NOT_FOUND", "terminal": True, "payer": {},
                "message": "Page de paiement abandonnée ou expirée : aucun paiement."}
    status = res["internal"]
    terminal = status in client.TERMINAL
    if terminal:
        client.registry.deliver(provider_id, {"status": status, "raw": res["raw"],
                                              "failure": res["failure"]})
    detail = client.failure_message(res["failure"]) if status in ("failed", "cancelled") else ""
    return {"status": status, "raw": res["raw"], "terminal": terminal,
            "payer": _payer(res.get("data") or {}),
            "message": detail or f"Statut pawaPay : {res['raw']}"}
