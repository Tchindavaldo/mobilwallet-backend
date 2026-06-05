"""Routes transactions : historique, statut, trace IA, erreurs, déblocage."""

from fastapi import APIRouter, HTTPException

from core.db import db

router = APIRouter()


@router.get("/transactions", tags=["admin"], summary="Historique des transactions (admin)",
            include_in_schema=False)
async def list_transactions(aggregator: str | None = None, limit: int = 50):
    """Dernières transactions auditées (vide si Supabase non configuré).

    Filtre optionnel par `aggregator`.
    """
    return {"transactions": await db.list_transactions(aggregator=aggregator, limit=limit)}


@router.get(
    "/status/{transaction_ref}",
    tags=["admin"],
    summary="Statut d'une transaction (admin)",
    include_in_schema=False,
    responses={404: {"description": "Aucune transaction pour cette référence."}},
)
async def transaction_status(transaction_ref: str):
    """Récupère une transaction stockée par sa référence."""
    tx = await db.get_transaction(transaction_ref)
    if tx is None:
        raise HTTPException(404, f"No transaction found for ref '{transaction_ref}'")
    return tx


@router.get(
    "/transactions/{transaction_ref}/trace",
    tags=["admin"],
    summary="Trace IA d'une transaction (admin, mode browser)",
    include_in_schema=False,
    responses={404: {"description": "Aucune transaction pour cette référence."}},
)
async def transaction_trace(transaction_ref: str):
    """Renvoie la trace tour-par-tour de l'agent IA pour une transaction browser.

    Chaque entrée : ce que l'IA a vu (url, nb d'éléments), sa pensée, les actions
    jouées, et si l'objectif a été atteint. Vide pour un paiement en mode replay.
    """
    tx = await db.get_transaction(transaction_ref)
    if tx is None:
        raise HTTPException(404, f"No transaction found for ref '{transaction_ref}'")
    traces = await db.get_traces(tx["id"])
    return {"transaction_ref": transaction_ref, "turns": len(traces), "trace": traces}


@router.get(
    "/transactions/{transaction_ref}/errors",
    tags=["admin"],
    summary="Erreurs détaillées d'une transaction (admin, browser/replay)",
    include_in_schema=False,
    responses={404: {"description": "Aucune transaction pour cette référence."}},
)
async def transaction_errors(transaction_ref: str):
    """Renvoie les erreurs détaillées d'une transaction.

    Chaque entrée précise le moteur (`engine`: browser/replay) et la SOURCE
    (`source`: ai / browser / transaction / replay) pour savoir exactement où ça
    a cassé, plus la catégorie, le message clair et les détails. Vide si la
    transaction a réussi.
    """
    tx = await db.get_transaction(transaction_ref)
    if tx is None:
        raise HTTPException(404, f"No transaction found for ref '{transaction_ref}'")
    errors = await db.get_errors(tx["id"])
    return {"transaction_ref": transaction_ref, "count": len(errors), "errors": errors}


@router.post(
    "/transactions/{tx_id}/cancel",
    tags=["transactions"],
    summary="Débloquer une transaction pending",
    responses={
        404: {"description": "Aucune transaction 'pending' avec cet id."},
        503: {"description": "Supabase non configuré."},
    },
)
async def cancel_transaction(tx_id: int):
    """Force une transaction restée **pending** à **cancelled**.

    Sert à débloquer une transaction coincée (ex: serveur interrompu avant le
    verdict) qui empêcherait un nouveau paiement sur le même numéro
    (`409 pending_exists`). N'agit que sur une ligne encore `pending` — ne
    réécrit jamais un verdict déjà acté.
    """
    if not db.enabled:
        raise HTTPException(503, "Supabase non configuré.")
    row = await db.cancel_pending(tx_id)
    if row is None:
        raise HTTPException(404, f"Aucune transaction 'pending' avec l'id {tx_id}.")
    return {"cancelled": row}
