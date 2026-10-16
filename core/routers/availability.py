"""Route disponibilité : état temps réel des opérateurs (MTN, Orange…) chez un
agrégateur et montant minimum d'un dépôt, pour savoir AVANT un /pay ou /payout
si l'opération passera."""

from fastapi import APIRouter, Depends, HTTPException, Query

from core import aggregator_ops, availability, deposit_limits, registry
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
    OPERATIONAL (normal), DELAYED (payouts/refunds mis en file : acceptés,
    exécutés plus tard), CLOSED (refusé : utilisez l'autre réseau).
    Liste vide si l'agrégateur ne publie pas cette information."""
    if aggregator not in registry.names():
        raise HTTPException(404, {"error": "unknown_aggregator",
                                  "supported_aggregators": registry.names()})
    rows = await availability.list_availability(aggregator)
    if rows is None:
        raise HTTPException(502, "Agrégateur injoignable, réessayez.")
    return {"aggregator": aggregator, "operators": rows}


@router.get(
    "/pay/minimum",
    tags=["availability"],
    summary="Montant minimum (et maximum) d'un dépôt",
    responses={404: {"description": "Agrégateur inconnu."}},
)
async def deposit_minimum(aggregator: str = Query("pawapay"),
                          network: str | None = Query(None, description="MTN | Orange ; vide = page de paiement"),
                          ctx: AuthContext = Depends(require_api_key)):
    """`min` : en dessous, /pay et /pay/page refusent (422 amount_too_low).
    C'est le plus grand entre le minimum de l'agrégateur et le nôtre (montant qui
    laisse au moins 1 F à votre app après frais, commission et réserve de
    payout, tous arrondis au franc supérieur). Recalculé selon la config."""
    if aggregator not in registry.names():
        raise HTTPException(404, {"error": "unknown_aggregator",
                                  "supported_aggregators": registry.names()})
    return {"aggregator": aggregator,
            **await deposit_limits.limits(aggregator, network, ctx.app_id)}


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
