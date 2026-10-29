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

from core import availability, registry, tenants
from core.auth import AuthContext, require_api_key
from core.config import settings
from core import settlement, status_check
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

# Agrégateurs supportant le payout -> module exposant initiate_payout /
# poll_payout_status / friendly_msg / PayoutUpstreamError (même interface).
_PAYOUT_FLOWS = {
    "digikuntz": "aggregators.digikuntz.payout_flow",
    "pawapay": "aggregators.pawapay.payout_flow",
}


def _flow(aggregator: str):
    """Module payout_flow de l'agrégateur (import paresseux)."""
    import importlib
    return importlib.import_module(_PAYOUT_FLOWS[aggregator])


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
    if req.aggregator not in _PAYOUT_FLOWS:
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
    op_status = await availability.guard(req.aggregator, req.account_bank_code, "payout")
    resp = await _run_payout(req, app_id, api_key_id, debug)
    if op_status == "DELAYED" and isinstance(resp, PayoutResponse) and resp.status == "pending":
        resp.message = availability.delayed_message(req.account_bank_code)
        resp.dev_message = availability.DELAYED_DEV_MESSAGE
    return resp


async def _run_payout(req, app_id, api_key_id, debug):
    # Mode mock : court-circuite DigiKUNTZ (garde quand même la compta réelle).
    if settings.mock_payments:
        return await _execute_payout_mock(req, app_id, api_key_id, debug)

    tx_id, balance_after = await _prepare_payout(req, app_id, api_key_id)
    # 2) Initiation du virement chez DigiKUNTZ.
    payout_flow = _flow(req.aggregator)
    try:
        init = await payout_flow.initiate_payout(req)
    except payout_flow.PayoutUpstreamError as e:
        # Panne amont : rien n'est parti -> on rembourse le débit et on 503.
        await _refund(app_id, req, tx_id)
        await db.update_payout(
            tx_id, status="failed", success=False,
            message=UPSTREAM_MESSAGES[e.code])
        raise HTTPException(503, {"code": e.code, "message": UPSTREAM_MESSAGES[e.code]})
    except Exception as e:  # noqa: BLE001 — refus/erreur non-amont
        log.warning("payout tx=%s: initiation échouée (%s)", tx_id, e)
        await _refund(app_id, req, tx_id)
        await db.update_payout(
            tx_id, status="failed", success=False,
            message="Le virement n'a pas pu être initié.")
        raise HTTPException(502, {"error": "payout_failed",
                                  "message": "Le virement n'a pas pu être initié."})

    return await _apply_init(req, app_id, tx_id, init, balance_after, debug)


async def _prepare_payout(req: PayoutRequest, app_id: int, api_key_id: int | None):
    """Insère le payout pending et réserve le solde (montant + frais imputés).
    Retourne (tx_id, balance_after) ; lève 422 si solde insuffisant."""
    tx_id = await db.insert_pending_payout(
        req.aggregator, req, app_id=app_id, api_key_id=api_key_id,
        end_user_ref=req.end_user_ref, callback_url=req.callback_url,
    )

    # 1) Réservation atomique du solde (débit = montant + frais agrégateur si
    #    imputés à l'app). Refus AVANT tout appel à l'agrégateur.
    charges = await settlement.payout_charges(req.aggregator, req.account_bank_code, req.amount)
    if charges["provider_fee"]:
        await db.set_provider_fee(tx_id, charges["provider_fee"])
    reserved = await settlement.reserve_app_payout(app_id, req.aggregator, req.account_bank_code,
                                                   req.amount, tx_id, req.currency)
    if not reserved or not reserved.get("ok"):
        balance = (reserved or {}).get("balance", 0)
        needed = (reserved or {}).get("needed", req.amount + charges["provider_fee"])
        await db.update_payout(
            tx_id, status="failed", success=False,
            message="Solde insuffisant pour ce virement.")
        raise HTTPException(
            422,
            {"error": "insufficient_balance",
             "message": (f"Solde insuffisant chez {req.aggregator} pour effectuer ce "
                         f"virement (les soldes sont séparés par agrégateur)."),
             "aggregator": req.aggregator,
             "balance": balance, "requested": needed,
             "fee": (reserved or {}).get("fee_from_balance", 0), "currency": req.currency},
        )
    balance_after = reserved.get("balance_after")
    _FEES[tx_id] = int(reserved.get("fee_from_balance") or 0)
    if charges["platform_extra"]:
        await db.debit_platform(charges["platform_extra"], transaction_id=tx_id,
                                reason="payout_provider_fee")

    return tx_id, balance_after


async def _apply_init(req, app_id, tx_id, init, balance_after, debug):
    """Applique le résultat d'initiation (unitaire ou bulk) : BD, verdict
    immédiat éventuel, sinon finalisation en tâche de fond."""
    payout_flow = _flow(req.aggregator)
    provider_id = init["provider_id"]
    transaction_ref = init["transaction_ref"]
    internal = init["internal_status"]
    await db.update_payout(
        tx_id, status=internal if internal in ("pending",) else internal,
        success=(internal == "successful"),
        message=payout_flow.friendly_msg(internal),
        provider_id=provider_id, raw=init["raw_status"],
        transaction_ref=transaction_ref)

    # 3a) Verdict terminal immédiat (rare) : settle + notifie + rembourse si échec.
    if internal in ("successful", "failed", "cancelled"):
        if internal != "successful":
            await _refund(app_id, req, tx_id)
        notify_settled(
            tx_id, internal, transaction_ref=transaction_ref,
            amount=req.amount, network=req.account_bank_code,
            phone=req.account_number, end_user_ref=req.end_user_ref,
            provider_transaction_id=provider_id, callback_url=req.callback_url)
        return _render(req, internal, payout_flow.friendly_msg(internal),
                       transaction_ref, provider_id, init["raw_status"],
                       balance_after, debug, tx_id)

    # 3b) pending : on rend la main et on finalise (poll -> verdict) en fond.
    asyncio.create_task(_finalize_payout_bg(req, tx_id, app_id, provider_id,
                                            transaction_ref))
    return _render(req, "pending", "Virement en cours de traitement.",
                   transaction_ref, provider_id, init["raw_status"],
                   balance_after, debug, tx_id)


async def _refund(app_id: int, req: PayoutRequest, tx_id: int) -> None:
    """Rembourse ce qu'un payout échoué a débité (solde + réserves, cf.
    core.settlement). Idempotent."""
    charges = await settlement.payout_charges(req.aggregator, req.account_bank_code, req.amount)
    await settlement.refund_payout({
        "app_id": app_id, "id": tx_id, "amount": req.amount,
        "provider_fee": charges["provider_fee"], "aggregator": req.aggregator})


# ---------------------------------------------------------------------------
# Payout PLATEFORME — retrait sur l'argent propre de MobileWallet (marge/frais/
# flottant), distinct des apps clientes. Réservé à l'admin. PAS de contrôle de
# solde (admin de confiance) : le solde plateforme peut devenir négatif et se
# recharger via POST /admin/platform/credit. Même suivi (polling auto + webhook).
# ---------------------------------------------------------------------------
async def _platform_total(req: PayoutRequest) -> int:
    """Coût d'un payout plateforme : montant + frais agrégateur pris sur le wallet."""
    charges = await settlement.payout_charges(req.aggregator, req.account_bank_code, req.amount)
    return req.amount + charges["provider_fee"]


async def execute_platform_payout(
    req: PayoutRequest, debug: bool = False,
) -> PayoutResponse | PayoutResponseDebug:
    """Exécute un payout débité du solde PLATEFORME (pas d'une app)."""
    _validate_payout_aggregator_and_network(req)
    await _guard_duplicate_payout(req)
    await availability.guard(req.aggregator, req.account_bank_code, "payout")

    if settings.mock_payments:
        return await _platform_payout_mock(req, debug)

    tx_id = await db.insert_pending_payout(req.aggregator, req, callback_url=req.callback_url)
    # Débit plateforme SANS contrôle de solde (peut devenir négatif).
    await db.debit_platform(await _platform_total(req), transaction_id=tx_id, currency=req.currency)
    balance_after = await db.get_platform_balance()

    payout_flow = _flow(req.aggregator)
    try:
        init = await payout_flow.initiate_payout(req)
    except payout_flow.PayoutUpstreamError as e:
        await db.credit_platform(await _platform_total(req), reason="payout_refund", transaction_id=tx_id)
        await db.update_payout(tx_id, status="failed", success=False,
                               message=UPSTREAM_MESSAGES[e.code])
        raise HTTPException(503, {"code": e.code, "message": UPSTREAM_MESSAGES[e.code]})
    except Exception as e:  # noqa: BLE001
        log.warning("platform payout tx=%s: initiation échouée (%s)", tx_id, e)
        await db.credit_platform(await _platform_total(req), reason="payout_refund", transaction_id=tx_id)
        await db.update_payout(tx_id, status="failed", success=False,
                               message="Le virement n'a pas pu être initié.")
        raise HTTPException(502, {"error": "payout_failed",
                                  "message": "Le virement n'a pas pu être initié."})

    provider_id = init["provider_id"]
    transaction_ref = init["transaction_ref"]
    internal = init["internal_status"]
    await db.update_payout(tx_id, status=internal, success=(internal == "successful"),
                           message=payout_flow.friendly_msg(internal),
                           provider_id=provider_id, raw=init["raw_status"],
                           transaction_ref=transaction_ref)

    if internal in ("successful", "failed", "cancelled"):
        if internal != "successful":
            await db.credit_platform(await _platform_total(req), reason="payout_refund", transaction_id=tx_id)
        notify_settled(tx_id, internal, transaction_ref=transaction_ref,
                       amount=req.amount, network=req.account_bank_code,
                       phone=req.account_number, provider_transaction_id=provider_id,
                       callback_url=req.callback_url)
        return _render(req, internal, payout_flow.friendly_msg(internal),
                       transaction_ref, provider_id, init["raw_status"],
                       balance_after, debug, tx_id)

    asyncio.create_task(_finalize_platform_payout_bg(req, tx_id, provider_id, transaction_ref))
    return _render(req, "pending", "Virement plateforme en cours de traitement.",
                   transaction_ref, provider_id, init["raw_status"], balance_after, debug)


async def _finalize_platform_payout_bg(req, tx_id, provider_id, transaction_ref):
    """Phase 2 du payout plateforme : poll -> settle -> rembourse plateforme si échec
    -> notifie."""
    payout_flow = _flow(req.aggregator)
    try:
        verdict = await payout_flow.poll_payout_status(provider_id)
        status = verdict["status"]
        if status != "successful":
            await db.credit_platform(await _platform_total(req), reason="payout_refund", transaction_id=tx_id)
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
    tx_id = await db.insert_pending_payout(req.aggregator, req, callback_url=req.callback_url)
    await db.debit_platform(await _platform_total(req), transaction_id=tx_id, currency=req.currency)
    balance_after = await db.get_platform_balance()
    provider_id = f"mock_payout_{tx_id}"
    transaction_ref = f"mock_{tx_id}"
    scenario = "fail" if random.random() < 0.2 else "success"
    asyncio.create_task(_mock_finalize_platform(req, tx_id, scenario, provider_id, transaction_ref))
    return _render(req, "pending", "Virement plateforme en cours (mock).",
                   transaction_ref, provider_id, "payout_pending", balance_after, debug, tx_id)


async def _mock_finalize_platform(req, tx_id, scenario, provider_id, transaction_ref):
    import random
    payout_flow = _flow(req.aggregator)
    await asyncio.sleep(random.randint(1, 3))
    status = "successful" if scenario == "success" else "failed"
    if status != "successful":
        await db.credit_platform(await _platform_total(req), reason="payout_refund", transaction_id=tx_id)
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
    payout_flow = _flow(req.aggregator)
    try:
        verdict = await payout_flow.poll_payout_status(provider_id)
        status = verdict["status"]
        if status != "successful":
            await _refund(app_id, req, tx_id)
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
        end_user_ref=req.end_user_ref, callback_url=req.callback_url)
    reserved = await settlement.reserve_app_payout(app_id, req.aggregator, req.account_bank_code,
                                                   req.amount, tx_id, req.currency)
    if not reserved or not reserved.get("ok"):
        balance = (reserved or {}).get("balance", 0)
        await db.update_payout(tx_id, status="failed", success=False,
                               message="Solde insuffisant pour ce virement.")
        raise HTTPException(422, {"error": "insufficient_balance",
                                  "message": "Solde insuffisant pour effectuer ce virement.",
                                  "balance": balance, "requested": req.amount})
    balance_after = reserved.get("balance_after")
    _FEES[tx_id] = int(reserved.get("fee_from_balance") or 0)
    provider_id = f"mock_payout_{tx_id}"
    transaction_ref = f"mock_{tx_id}"
    scenario = "fail" if random.random() < 0.2 else "success"
    asyncio.create_task(_mock_finalize_payout(req, tx_id, app_id, scenario,
                                              provider_id, transaction_ref))
    return _render(req, "pending", "Virement en cours de traitement (mock).",
                   transaction_ref, provider_id, "payout_pending", balance_after, debug, tx_id)


async def _mock_finalize_payout(req, tx_id, app_id, scenario, provider_id, transaction_ref):
    import random
    payout_flow = _flow(req.aggregator)
    await asyncio.sleep(random.randint(1, 3))
    status = "successful" if scenario == "success" else "failed"
    if status != "successful":
        await _refund(app_id, req, tx_id)
    await db.update_payout(tx_id, status=status, success=(status == "successful"),
                           message=payout_flow.friendly_msg(status),
                           provider_id=provider_id, settled_by="polling")
    notify_settled(tx_id, status, transaction_ref=transaction_ref,
                   amount=req.amount, network=req.account_bank_code,
                   phone=req.account_number, end_user_ref=req.end_user_ref,
                   provider_transaction_id=provider_id, callback_url=req.callback_url)


# Frais pris sur le solde de l'app par payout (tx_id -> XAF), pour la réponse.
_FEES: dict[int, int] = {}


def _render(req, status, message, transaction_ref, provider_id, raw, balance_after, debug,
            tx_id: int | None = None):
    """Construit la réponse client (défaut) ou admin/debug (?debug=true)."""
    fee = _FEES.pop(tx_id, 0) if tx_id is not None else 0
    client = PayoutResponse(
        success=(status == "successful"), status=status, message=message,
        transaction_id=transaction_ref, code=200, fee=fee,
        total_debited=req.amount + fee)
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


@router.get(
    "/payout/{transaction_ref}/check",
    tags=["payout"],
    summary="Vérifier le statut d'un virement chez l'agrégateur",
    responses={404: {"description": "Aucun virement pour cette référence."},
               502: {"description": "Agrégateur injoignable, réessayez."}},
)
async def check_payout(transaction_ref: str, ctx: AuthContext = Depends(require_api_key)):
    """Interroge UNE fois l'agrégateur sur un virement de votre app (pas de
    polling : le verdict arrive normalement par webhook). Si le statut est final
    (`successful`/`failed`/`cancelled`), il est appliqué tout de suite
    (BD + remboursement si échec + notification)."""
    tx = await tenants.get_transaction_scoped(transaction_ref, ctx.app_id)
    if tx is None or (tx.get("type") or "").lower() != "payout":
        raise HTTPException(404, f"No payout found for ref '{transaction_ref}'")
    res = await status_check.check_transaction(tx)
    if res is None:
        raise HTTPException(502, "Agrégateur injoignable, réessayez.")
    return res
