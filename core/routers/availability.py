"""Route disponibilité : état temps réel des opérateurs (MTN, Orange…) chez un
agrégateur, pour savoir AVANT un /pay ou /payout si l'opération passera."""

from fastapi import APIRouter, Depends, HTTPException, Query

from core import aggregator_ops, availability, registry
from core.schemas.operations import PredictNetworkRequest
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


@router.post(
    "/operators/predict",
    tags=["availability"],
    summary="Deviner l'opérateur d'un numéro",
    responses={400: {"description": "Agrégateur sans prédiction d'opérateur."}},
)
async def predict_network(req: PredictNetworkRequest,
                          ctx: AuthContext = Depends(require_api_key)):
    """À partir d'un numéro saisi librement (espaces, +237, 0…), renvoie le
    numéro au format international et son opérateur (`network` utilisable
    tel quel dans /pay)."""
    return await aggregator_ops.call(req.aggregator, "predict_network", req.phone)
