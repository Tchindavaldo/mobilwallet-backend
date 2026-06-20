"""Route payout : POST /payout (virement sortant + comptabilité par app).

MobileWallet est lui-même un agrégateur : DigiKUNTZ n'a qu'UN compte global commun.
C'est donc nous qui tenons le solde de chaque app (grand livre app_ledger). Un
retrait d'une app DÉBITE son solde — jamais celui d'une autre. Invariant :
Σ soldes des apps == solde réel du compte global DigiKUNTZ.

Flux comptable : on RÉSERVE (débite) le solde à l'initiation (atomique, anti
double-dépense), puis on REMBOURSE si le virement échoue. Ainsi un payout en vol
bloque déjà les fonds et l'invariant tient à tout instant.
"""

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse

from core import registry
from core.auth import AuthContext, require_api_key
from core.config import settings
from core.db import db
from core.notifications import notify_settled
from core.schemas.payout import PayoutRequest, PayoutResponse, PayoutResponseDebug
from core.upstream_errors import UPSTREAM_MESSAGES

log = logging.getLogger("ai_browser2")

router = APIRouter()

# Codes accountBankCode acceptés AU PAYOUT — STRICT, aucune conversion ni variante.
# Le client doit envoyer EXACTEMENT l'une de ces valeurs (DigiKUNTZ les exige telles
# quelles). Différent du payin (qui tolère 'orange' -> 'Orangemoney').
_PAYOUT_BANK_CODES = ("ORANGEMONEY", "MTN")


def _validate_payout_aggregator_and_network(req: PayoutRequest) -> None:
    """Valide l'agrégateur et exige un account_bank_code STRICT (ORANGEMONEY|MTN),
    sans aucune tolérance/conversion. Lève 404/400/422 sinon."""
    if req.aggregator not in registry.names():
        raise HTTPException(
            404,
            {"error": "unknown_aggregator",
             "message": f"Agrégateur '{req.aggregator}' inconnu.",
             "supported_aggregators": registry.names()},
        )
    if req.aggregator != "digikuntz":
        raise HTTPException(
            400,
            {"error": "payout_unsupported",
             "message": f"L'agrégateur '{req.aggregator}' ne supporte pas les payouts."},
        )
    if req.account_bank_code not in _PAYOUT_BANK_CODES:
        raise HTTPException(
            422,
            {"error": "invalid_network",
             "message": f"account_bank_code doit être exactement l'un de "
                        f"{list(_PAYOUT_BANK_CODES)} (reçu : '{req.account_bank_code}').",
             "aggregator": req.aggregator,
             "supported_account_bank_codes": list(_PAYOUT_BANK_CODES)},
        )


async def _guard_duplicate_payout(req: PayoutRequest) -> None:
    """Garde anti-doublon : au plus un payout 'pending' par bénéficiaire."""
    last = await db.last_payout_for_account(req.aggregator, req.account_number)
    if not last:
        return
    if (last.get("status") or "").lower() == "pending":
        raise HTTPException(
            409,
            {
                "error": "pending_exists",
                "message": (
                    f"Un virement est déjà en cours (pending) vers le compte "
                    f"{req.account_number}. Veuillez attendre son aboutissement."
                ),
                "aggregator": req.aggregator,
                "account_number": req.account_number,
                "transaction_id": last.get("id"),
            },
        )


async def _execute_payout(
    req: PayoutRequest,
    app_id: int,
    api_key_id: int | None,
    debug: bool = False,
) -> PayoutResponse | PayoutResponseDebug:
    """Logique partagée d'exécution payout (réutilisée par /payout ET admin)."""
    _validate_payout_aggregator_and_network(req)
    await _guard_duplicate_payout(req)

    # Mode mock : court-circuite DigiKUNTZ (garde quand même la compta réelle).
    if settings.mock_payments:
        return await _execute_payout_mock(req, app_id, api_key_id, debug)

    tx_id = await db.insert_pending_payout(
        req.aggregator, req, app_id=app_id, api_key_id=api_key_id,
        end_user_ref=req.end_user_ref,
    )

    # 1) Réservation atomique du solde (débit). Refus AVANT tout appel DigiKUNTZ.
    reserved = await db.reserve_payout(app_id, req.amount, tx_id, req.currency)
    if not reserved or not reserved.get("ok"):
        balance = (reserved or {}).get("balance", 0)
        await db.update_payout(
            tx_id, status="failed", success=False,
            message="Solde insuffisant pour ce virement.")
        raise HTTPException(
            422,
            {"error": "insufficient_balance",
             "message": "Solde insuffisant pour effectuer ce virement.",
             "balance": balance, "requested": req.amount, "currency": req.currency},
        )
    balance_after = reserved.get("balance_after")

    # 2) Initiation du virement chez DigiKUNTZ.
    from aggregators.digikuntz import payout_flow
    try:
        init = await payout_flow.initiate_payout(req)
    except payout_flow.PayoutUpstreamError as e:
        # Panne amont : rien n'est parti -> on rembourse le débit et on 503.
        await _refund(app_id, req.amount, tx_id)
        await db.update_payout(
            tx_id, status="failed", success=False,
            message=UPSTREAM_MESSAGES[e.code])
        raise HTTPException(503, {"code": e.code, "message": UPSTREAM_MESSAGES[e.code]})
    except Exception as e:  # noqa: BLE001 — refus/erreur non-amont
        log.warning("payout tx=%s: initiation échouée (%s)", tx_id, e)
        await _refund(app_id, req.amount, tx_id)
        await db.update_payout(
            tx_id, status="failed", success=False,
            message="Le virement n'a pas pu être initié.")
        raise HTTPException(502, {"error": "payout_failed",
                                  "message": "Le virement n'a pas pu être initié."})

    provider_id = init["provider_id"]
    transaction_ref = init["transaction_ref"]
    internal = init["internal_status"]
    await db.update_payout(
        tx_id, status=internal if internal in ("pending",) else internal,
        success=(internal == "successful"),
        message=payout_flow.friendly_msg(internal),
        provider_id=provider_id, raw=init["raw_status"])

    # 3a) Verdict terminal immédiat (rare) : settle + notifie + rembourse si échec.
    if internal in ("successful", "failed", "cancelled"):
        if internal != "successful":
            await _refund(app_id, req.amount, tx_id)
        notify_settled(
            tx_id, internal, transaction_ref=transaction_ref,
            amount=req.amount, network=req.account_bank_code,
            phone=req.account_number, end_user_ref=req.end_user_ref,
            provider_transaction_id=provider_id, callback_url=req.callback_url)
        return _render(req, internal, payout_flow.friendly_msg(internal),
                       transaction_ref, provider_id, init["raw_status"],
                       balance_after, debug)

    # 3b) pending : on rend la main et on finalise (poll -> verdict) en fond.
    asyncio.create_task(_finalize_payout_bg(req, tx_id, app_id, provider_id,
                                            transaction_ref))
    return _render(req, "pending", "Virement en cours de traitement.",
                   transaction_ref, provider_id, init["raw_status"],
                   balance_after, debug)


async def _refund(app_id: int, amount: int, tx_id: int) -> None:
    """Rembourse (crédit compensatoire) le débit d'un payout échoué. Idempotent
    sur (transaction_id, direction='credit')."""
    await db.credit_app(app_id, amount, transaction_id=tx_id, reason="payout_refund")


# ---------------------------------------------------------------------------
# Payout PLATEFORME — retrait sur l'argent propre de MobileWallet (marge/frais/
# flottant), distinct des apps clientes. Réservé à l'admin. PAS de contrôle de
# solde (admin de confiance) : le solde plateforme peut devenir négatif et se
# recharger via POST /admin/platform/credit. Même suivi (polling auto + webhook).
# ---------------------------------------------------------------------------
async def execute_platform_payout(
    req: PayoutRequest, debug: bool = False,
) -> PayoutResponse | PayoutResponseDebug:
    """Exécute un payout débité du solde PLATEFORME (pas d'une app)."""
    _validate_payout_aggregator_and_network(req)
    await _guard_duplicate_payout(req)

    if settings.mock_payments:
        return await _platform_payout_mock(req, debug)

    tx_id = await db.insert_pending_payout(req.aggregator, req)
    # Débit plateforme SANS contrôle de solde (peut devenir négatif).
    await db.debit_platform(req.amount, transaction_id=tx_id, currency=req.currency)
    balance_after = await db.get_platform_balance()

    from aggregators.digikuntz import payout_flow
    try:
        init = await payout_flow.initiate_payout(req)
    except payout_flow.PayoutUpstreamError as e:
        await db.credit_platform(req.amount, reason="payout_refund", transaction_id=tx_id)
        await db.update_payout(tx_id, status="failed", success=False,
                               message=UPSTREAM_MESSAGES[e.code])
        raise HTTPException(503, {"code": e.code, "message": UPSTREAM_MESSAGES[e.code]})
    except Exception as e:  # noqa: BLE001
        log.warning("platform payout tx=%s: initiation échouée (%s)", tx_id, e)
        await db.credit_platform(req.amount, reason="payout_refund", transaction_id=tx_id)
        await db.update_payout(tx_id, status="failed", success=False,
                               message="Le virement n'a pas pu être initié.")
        raise HTTPException(502, {"error": "payout_failed",
                                  "message": "Le virement n'a pas pu être initié."})

    provider_id = init["provider_id"]
    transaction_ref = init["transaction_ref"]
    internal = init["internal_status"]
    await db.update_payout(tx_id, status=internal, success=(internal == "successful"),
                           message=payout_flow.friendly_msg(internal),
                           provider_id=provider_id, raw=init["raw_status"])

    if internal in ("successful", "failed", "cancelled"):
        if internal != "successful":
            await db.credit_platform(req.amount, reason="payout_refund", transaction_id=tx_id)
        notify_settled(tx_id, internal, transaction_ref=transaction_ref,
                       amount=req.amount, network=req.account_bank_code,
                       phone=req.account_number, provider_transaction_id=provider_id,
                       callback_url=req.callback_url)
        return _render(req, internal, payout_flow.friendly_msg(internal),
                       transaction_ref, provider_id, init["raw_status"],
                       balance_after, debug)

    asyncio.create_task(_finalize_platform_payout_bg(req, tx_id, provider_id, transaction_ref))
    return _render(req, "pending", "Virement plateforme en cours de traitement.",
                   transaction_ref, provider_id, init["raw_status"], balance_after, debug)


async def _finalize_platform_payout_bg(req, tx_id, provider_id, transaction_ref):
    """Phase 2 du payout plateforme : poll -> settle -> rembourse plateforme si échec
    -> notifie."""
    from aggregators.digikuntz import payout_flow
    try:
        verdict = await payout_flow.poll_payout_status(provider_id)
        status = verdict["status"]
        if status != "successful":
            await db.credit_platform(req.amount, reason="payout_refund", transaction_id=tx_id)
        await db.update_payout(tx_id, status=status, success=(status == "successful"),
                               message=verdict["message"], provider_id=provider_id,
                               settled_by=verdict.get("settled_by", "polling"))
        notify_settled(tx_id, status, transaction_ref=transaction_ref,
                       amount=req.amount, network=req.account_bank_code,
                       phone=req.account_number, provider_transaction_id=provider_id,
                       callback_url=req.callback_url)
    except Exception as e:  # noqa: BLE001
        log.exception("finalize_platform_payout_bg tx=%s a échoué: %s", tx_id, e)


async def _platform_payout_mock(req, debug):
    """Mode mock du payout plateforme (sans DigiKUNTZ, vraie compta plateforme)."""
    import random
    tx_id = await db.insert_pending_payout(req.aggregator, req)
    await db.debit_platform(req.amount, transaction_id=tx_id, currency=req.currency)
    balance_after = await db.get_platform_balance()
    provider_id = f"mock_payout_{tx_id}"
    transaction_ref = f"mock_{tx_id}"
    scenario = "fail" if random.random() < 0.2 else "success"
    asyncio.create_task(_mock_finalize_platform(req, tx_id, scenario, provider_id, transaction_ref))
    return _render(req, "pending", "Virement plateforme en cours (mock).",
                   transaction_ref, provider_id, "payout_pending", balance_after, debug)


async def _mock_finalize_platform(req, tx_id, scenario, provider_id, transaction_ref):
    import random
    from aggregators.digikuntz import payout_flow
    await asyncio.sleep(random.randint(1, 3))
    status = "successful" if scenario == "success" else "failed"
    if status != "successful":
        await db.credit_platform(req.amount, reason="payout_refund", transaction_id=tx_id)
    await db.update_payout(tx_id, status=status, success=(status == "successful"),
                           message=payout_flow.friendly_msg(status),
                           provider_id=provider_id, settled_by="polling")
    notify_settled(tx_id, status, transaction_ref=transaction_ref,
                   amount=req.amount, network=req.account_bank_code,
                   phone=req.account_number, provider_transaction_id=provider_id,
                   callback_url=req.callback_url)


async def _finalize_payout_bg(req, tx_id, app_id, provider_id, transaction_ref):
    """Phase 2 (hors requête) : poll le verdict -> settle BD -> rembourse si échec
    -> notifie le client (webhook + Socket.IO)."""
    from aggregators.digikuntz import payout_flow
    try:
        verdict = await payout_flow.poll_payout_status(provider_id)
        status = verdict["status"]
        if status != "successful":
            await _refund(app_id, req.amount, tx_id)
        await db.update_payout(
            tx_id, status=status, success=(status == "successful"),
            message=verdict["message"], provider_id=provider_id,
            settled_by=verdict.get("settled_by", "polling"))
        notify_settled(
            tx_id, status, transaction_ref=transaction_ref,
            amount=req.amount, network=req.account_bank_code,
            phone=req.account_number, end_user_ref=req.end_user_ref,
            provider_transaction_id=provider_id, callback_url=req.callback_url)
    except Exception as e:  # noqa: BLE001
        log.exception("finalize_payout_bg tx=%s a échoué: %s", tx_id, e)


async def _execute_payout_mock(req, app_id, api_key_id, debug):
    """Mode mock : simule le virement sans DigiKUNTZ, garde la compta réelle."""
    import random
    tx_id = await db.insert_pending_payout(
        req.aggregator, req, app_id=app_id, api_key_id=api_key_id,
        end_user_ref=req.end_user_ref)
    reserved = await db.reserve_payout(app_id, req.amount, tx_id, req.currency)
    if not reserved or not reserved.get("ok"):
        balance = (reserved or {}).get("balance", 0)
        await db.update_payout(tx_id, status="failed", success=False,
                               message="Solde insuffisant pour ce virement.")
        raise HTTPException(422, {"error": "insufficient_balance",
                                  "message": "Solde insuffisant pour effectuer ce virement.",
                                  "balance": balance, "requested": req.amount})
    balance_after = reserved.get("balance_after")
    provider_id = f"mock_payout_{tx_id}"
    transaction_ref = f"mock_{tx_id}"
    scenario = "fail" if random.random() < 0.2 else "success"
    asyncio.create_task(_mock_finalize_payout(req, tx_id, app_id, scenario,
                                              provider_id, transaction_ref))
    return _render(req, "pending", "Virement en cours de traitement (mock).",
                   transaction_ref, provider_id, "payout_pending", balance_after, debug)


async def _mock_finalize_payout(req, tx_id, app_id, scenario, provider_id, transaction_ref):
    import random
    from aggregators.digikuntz import payout_flow
    await asyncio.sleep(random.randint(1, 3))
    status = "successful" if scenario == "success" else "failed"
    if status != "successful":
        await _refund(app_id, req.amount, tx_id)
    await db.update_payout(tx_id, status=status, success=(status == "successful"),
                           message=payout_flow.friendly_msg(status),
                           provider_id=provider_id, settled_by="polling")
    notify_settled(tx_id, status, transaction_ref=transaction_ref,
                   amount=req.amount, network=req.account_bank_code,
                   phone=req.account_number, end_user_ref=req.end_user_ref,
                   provider_transaction_id=provider_id, callback_url=req.callback_url)


def _render(req, status, message, transaction_ref, provider_id, raw, balance_after, debug):
    """Construit la réponse client (défaut) ou admin/debug (?debug=true)."""
    client = PayoutResponse(
        success=(status == "successful"), status=status, message=message,
        transaction_id=transaction_ref, code=200)
    if not debug:
        return client
    full = PayoutResponseDebug(
        **client.model_dump(), provider_transaction_id=provider_id,
        raw_status=raw, balance_after=balance_after)
    return JSONResponse(content=full.model_dump())


@router.post(
    "/payout",
    response_model=PayoutResponse,
    tags=["payout"],
    summary="Effectuer un virement (payout)",
    responses={
        404: {"description": "Agrégateur inconnu."},
        400: {"description": "Agrégateur sans support payout."},
        409: {"description": "Un virement est déjà en cours vers ce bénéficiaire."},
        422: {"description": "Solde de l'app insuffisant pour ce virement."},
        502: {"description": "Le virement n'a pas pu être initié."},
        503: {"description": "Service de paiement amont temporairement indisponible."},
    },
)
async def payout(
    req: PayoutRequest,
    ctx: AuthContext = Depends(require_api_key),
    debug: bool = Query(False, include_in_schema=False),
):
    """Effectue un virement sortant débité du solde de VOTRE app.

    Le solde de l'app = somme de ses encaissements réussis − ses retraits. Un
    virement supérieur au solde disponible est refusé (422) avant tout appel amont.
    Le verdict final (`successful`/`failed`/`cancelled`) est poussé via webhook +
    Socket.IO ; consultable aussi par `GET /status/{transaction_id}`.
    """
    return await _execute_payout(req, ctx.app_id, ctx.api_key_id, debug)
