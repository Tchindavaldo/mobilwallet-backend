"""Relevés d'agrégateur stockés chez nous (admin) :
- POST /admin/{aggregator}/statements/sync : synchro à la demande (période libre) ;
- GET  /admin/{aggregator}/statements      : lignes stockées, paginées.
Neutre : délègue à `aggregators.<nom>.statements` (400 si absent)."""

import importlib
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query

from core.auth import require_admin

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_admin)],
                   responses={401: {"description": "X-Admin-Key invalide ou manquant."}})


def _mod(aggregator: str):
    try:
        return importlib.import_module(f"aggregators.{aggregator}.statements")
    except ModuleNotFoundError:
        raise HTTPException(400, {"error": "operation_unsupported",
                                  "message": f"Pas de relevés pour '{aggregator}'."})


def _date(s: str) -> datetime:
    d = datetime.fromisoformat(s)
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


@router.post("/{aggregator}/statements/sync", summary="Synchroniser les relevés de l'agrégateur")
async def sync_statements(aggregator: str,
                          start_date: str | None = Query(None, description="YYYY-MM-DD ; "
                                                         "vide = depuis la dernière synchro"),
                          end_date: str | None = Query(None, description="YYYY-MM-DD ; vide = maintenant")):
    """Télécharge le relevé de la période (par tranches de 31 j) et ajoute les
    lignes nouvelles en base. Sans dates : depuis la dernière synchro (avec 2 j
    de recouvrement pour les lignes arrivées en retard)."""
    mod = _mod(aggregator)
    if not start_date:
        return await mod.sync_since_last()
    end = _date(end_date) if end_date else datetime.now(timezone.utc)
    return await mod.sync(_date(start_date), end)


@router.get("/{aggregator}/statements", summary="Lignes de relevé stockées (paginé)")
async def list_statements(aggregator: str, page: int = Query(1, ge=1),
                          limit: int = Query(20, ge=1, le=100),
                          start_date: str | None = Query(None, description="YYYY-MM-DD"),
                          end_date: str | None = Query(None, description="YYYY-MM-DD"),
                          msisdn: str | None = Query(None, description="fin du numéro")):
    """Mouvements du wallet chez l'agrégateur (montants, frais, solde après),
    plus récents d'abord. Les paiements échoués n'y figurent pas (voir /transactions)."""
    res = await _mod(aggregator).list_lines(page, limit, start_date, end_date, msisdn)
    if res is None:
        raise HTTPException(503, "Supabase non configuré.")
    return res
