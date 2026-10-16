"""Route disponibilité : état temps réel des opérateurs (MTN, Orange…) chez un
agrégateur, pour savoir AVANT un /pay ou /payout si l'opération passera."""

from fastapi import APIRouter, Depends, HTTPException, Query

from core import availability, registry
from core.auth import AuthContext, require_api_key

router = APIRouter()


@router.get(
    "/availability",
    tags=["availability"],
    summary="Disponibilité des opérateurs",
    responses={404: {"description": "Agrégateur inconnu."},
               502: {"description": "Agrégateur injoignable, réessayez."}},
)
async def operators_availability(aggregator: str = Query("pawapay"),
                                 ctx: AuthContext = Depends(require_api_key)):
    """Statut par opérateur et par opération (deposit, payout, refund) :
    OPERATIONAL (normal), DELAYED (très lent : refusé), CLOSED (refusé).
    Liste vide si l'agrégateur ne publie pas cette information."""
    if aggregator not in registry.names():
        raise HTTPException(404, {"error": "unknown_aggregator",
                                  "supported_aggregators": registry.names()})
    rows = await availability.list_availability(aggregator)
    if rows is None:
        raise HTTPException(502, "Agrégateur injoignable, réessayez.")
    return {"aggregator": aggregator, "operators": rows}
