"""Route page de paiement hébergée : POST /pay/page.

L'agrégateur héberge la page (choix de l'opérateur, numéro, validation) ; on
rend `redirect_url` au client. Le verdict arrive par webhook comme un dépôt
classique (core.settlement) ; réseau et numéro sont complétés à ce moment-là."""

from fastapi import APIRouter, Depends, HTTPException

from core import aggregator_ops, registry, status_check
from core.auth import AuthContext, require_api_key
from core.base import PaymentRequest
from core.db import db
from core.schemas.operations import PaymentPageRequest

router = APIRouter()


@router.post(
    "/pay/page",
    tags=["payments"],
    summary="Créer une page de paiement hébergée",
    responses={400: {"description": "Agrégateur sans page hébergée."},
               502: {"description": "Page refusée par l'agrégateur."},
               503: {"description": "Service de paiement amont indisponible."}},
)
async def payment_page(req: PaymentPageRequest, ctx: AuthContext = Depends(require_api_key)):
    """Crée une page de paiement chez l'agrégateur et renvoie `redirect_url`
    où envoyer votre client. Suivi : `GET /status/{transaction_id}`, webhook,
    Socket.IO ; le client revient ensuite sur `return_url`.

    À l'arrivée sur `return_url`, ne considérez PAS le paiement comme fait :
    lisez `GET /status/{transaction_id}` (ou `/check`). Une page abandonnée
    passe `failed` environ 16 min après sa création."""
    if req.aggregator not in registry.names():
        raise HTTPException(404, {"error": "unknown_aggregator",
                                  "supported_aggregators": registry.names()})
    aggregator_ops.get(req.aggregator, "create_payment_page")
    app_name = ctx.app_name
    label = req.description or (f"MobileWallet {app_name}" if app_name else "MobileWallet")
    payment = PaymentRequest(amount=req.amount, phone=req.phone, network="",
                             email=req.email, sender_name=label,
                             callback_url=req.callback_url, raison=label)
    tx_id = await db.insert_pending(req.aggregator, "page", payment, app_id=ctx.app_id,
                                    api_key_id=ctx.api_key_id, end_user_ref=req.end_user_ref,
                                    callback_url=req.callback_url)
    res = await aggregator_ops.call(req.aggregator, "create_payment_page", req.amount,
                                    label, req.return_url, req.phone, req.language)
    if not res.get("redirect_url"):
        await db.set_initiated(tx_id, res["provider_id"], "failed",
                               "Page de paiement refusée par le prestataire.")
        raise HTTPException(502, {"error": "payment_page_rejected",
                                  "failure": res.get("failure")})
    await db.set_initiated(tx_id, res["provider_id"], "pending",
                           "Page de paiement créée : en attente du client.")
    # Page abandonnée = aucun webhook : une vérification après l'expiration
    # la clôture (et rattrape un webhook perdu si le client a payé).
    status_check.schedule_check(res["provider_id"],
                                aggregator_ops.get(req.aggregator, "PAGE_EXPIRY_S") + 60)
    return {"success": True, "status": "pending", "transaction_id": res["provider_id"],
            "redirect_url": res["redirect_url"]}
