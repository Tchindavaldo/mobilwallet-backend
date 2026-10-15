"""Route paiement : POST /pay (dispatch agrégateur, garde anti-doublon, settle)."""

import logging

from fastapi import APIRouter, Depends, HTTPException, Query

from core import registry, runtime
from core.auth import AuthContext, require_api_key
from core.base import PaymentRequest, PaymentResult
from core.notifications import notify_settled
from core.config import settings
from core.db import db
from core.routers.payments_common import (
    _credit_on_success, _guard_duplicate, _render_response, _settle,
)
from core.schemas.payments import PayRequest, PayResponse, PayResponseDebug
from core.upstream_errors import UPSTREAM_CODES, UPSTREAM_MESSAGES

log = logging.getLogger("ai_browser2")

router = APIRouter()


async def _execute_payment_mock(
    req: PayRequest,
    app_id: int,
    api_key_id: int | None,
    debug: bool = False,
) -> PayResponse | PayResponseDebug:
    """Mode mock : simule un paiement sans appeler DigiKUNTZ.

    Retourne ussd_sent immédiatement, puis déclenche une tâche de fond qui attend
    et envoie le verdict final via Socket + webhook après délai (1-3s).
    """
    log.info("MOCK /pay reçu: amount=%s, phone=%s, network=%s, callback_url=%s, end_user_ref=%s",
             req.amount, req.phone, req.network, req.callback_url or "(vide)", req.end_user_ref)

    from core import mock_aggregator

    payment = PaymentRequest(
        amount=req.amount,
        phone=req.phone,
        network=req.network,
        email=req.email,
        sender_name=req.sender_name,
        callback_url=req.callback_url,
    )

    await _guard_duplicate(req)

    # Scenario aléatoire : 80% success, 20% cancel (pour tester les deux chemins)
    scenario = "cancel" if __import__("random").random() < 0.2 else "success"

    tx_id = await db.insert_pending(
        "mock", "mock", payment,
        app_id=app_id, api_key_id=api_key_id, end_user_ref=req.end_user_ref,
    )

    # Phase 1 : retour immédiat "USSD envoyé"
    ussd_sent_result = PaymentResult(
        final_status="ussd_sent",
        transaction_id=f"mock_{tx_id}_{__import__('time').time_ns() // 1_000_000}",
        provider_transaction_id=f"mock_{tx_id}",
        success=False,
    )
    ussd_sent_result = _settle(ussd_sent_result, "mock")

    # Déclenche la phase 2 en arrière-plan (sans bloquer)
    __import__("asyncio").create_task(
        _mock_finalize_after_delay(
            tx_id, scenario, req,
            app_id, api_key_id
        )
    )

    return _render_response(ussd_sent_result, debug)


async def _mock_finalize_after_delay(
    tx_id: int,
    scenario: str,
    req: PayRequest,
    app_id: int,
    api_key_id: int | None,
) -> None:
    """Phase 2 du mock : attend puis envoie le verdict final via Socket + webhook."""
    from core import mock_aggregator
    import asyncio

    payment = PaymentRequest(
        amount=req.amount,
        phone=req.phone,
        network=req.network,
        email=req.email,
        sender_name=req.sender_name,
        callback_url=req.callback_url,
    )

    # Attendre 1-3s
    delay_s = __import__("random").randint(1, 3)
    await asyncio.sleep(delay_s)

    # Générer le verdict final
    result = await mock_aggregator.mock_pay_via_browser(payment, tx_id, scenario)
    result = _settle(result, "mock")

    # Mettre à jour la BD avec le verdict final
    await db.update_transaction(tx_id, result)

    # Crédite le solde de l'app si succès (idempotent), puis notifie.
    await _credit_on_success(app_id, tx_id, result, req.amount, req.aggregator)

    # Envoyer les notifications (Socket + webhook)
    notify_settled(
        tx_id, result, transaction_ref=result.transaction_id,
        amount=req.amount, network=req.network, phone=req.phone,
        end_user_ref=req.end_user_ref,
        provider_transaction_id=result.provider_transaction_id,
        callback_url=req.callback_url,
    )


async def _execute_payment(
    req: PayRequest,
    app_id: int,
    api_key_id: int | None,
    debug: bool = False,
    app_name: str = "",
) -> PayResponse | PayResponseDebug:
    """Logique partagée d'exécution de paiement (réutilisée par /pay ET /admin/apps/{app_id}/pay).

    Paramètres :
      - req : requête paiement (amount, phone, network, aggregator, mode, etc.)
      - app_id : app pour laquelle le paiement est lancé
      - api_key_id : ID de la clé API (None si appelé par admin)
      - debug : inclure le détail technique dans la réponse
    """
    # Mode mock : court-circuite DigiKUNTZ
    if settings.mock_payments:
        return await _execute_payment_mock(req, app_id, api_key_id, debug)

    cls = registry.get(req.aggregator)
    if cls is None:
        raise HTTPException(
            404,
            {
                "error": "unknown_aggregator",
                "message": f"Agrégateur '{req.aggregator}' inconnu.",
                "supported_aggregators": registry.names(),
            },
        )
    # Agrégateur « API directe » (ex. pawaPay) : ni navigateur, ni replay.
    if getattr(cls, "direct_api", False):
        return await _execute_direct(cls, req, app_id, api_key_id, debug, app_name)

    browser = runtime.get_browser()
    llm = runtime.get_llm()
    if not browser or not llm:
        raise HTTPException(500, "Not initialized")

    if req.mode not in ("auto", "browser", "replay"):
        raise HTTPException(
            400,
            {
                "error": "invalid_mode",
                "message": f"Mode '{req.mode}' invalide.",
                "supported_modes": ["auto", "browser", "replay"],
            },
        )

    agg = cls(browser=browser, llm=llm, db=db)

    canonical_network = agg.normalize_network(req.network)
    if canonical_network is None:
        raise HTTPException(
            422,
            {
                "error": "invalid_network",
                "message": f"Réseau '{req.network}' non supporté par l'agrégateur '{req.aggregator}'.",
                "aggregator": req.aggregator,
                "supported_networks": agg.supported_networks,
            },
        )

    label = f"MobileWallet-{app_name}" if app_name else "MobileWallet"
    payment = PaymentRequest(
        amount=req.amount,
        phone=req.phone,
        network=canonical_network,
        email=req.email,
        sender_name=label,
        callback_url=req.callback_url,
        raison=label,
    )

    await _guard_duplicate(req)

    async def _run_browser_and_save():
        res = await agg.pay_via_browser(payment, tx_id=tx_id)
        if res.curl_template:
            await db.save_template(req.aggregator, res.curl_template)
        return res

    template = None
    if req.mode == "replay":
        template = await db.load_template(req.aggregator)
        if template is None:
            raise HTTPException(
                409,
                f"No stored curl template for '{req.aggregator}'. Run mode='browser' once to deduce it.",
            )
    elif req.mode == "auto":
        template = await db.load_template(req.aggregator)
        if template is None and not req.fallback_browser:
            raise HTTPException(
                409,
                f"No template for '{req.aggregator}' and fallback_browser=false. Run mode='browser' first.",
            )

    tx_id = await db.insert_pending(
        req.aggregator, req.mode, payment,
        app_id=app_id, api_key_id=api_key_id, end_user_ref=req.end_user_ref,
    )
    result = None
    engine_used = "replay"
    try:
        if req.mode == "replay":
            result = await agg.replay(payment, template)
            engine_used = "replay"
        elif req.mode == "browser":
            result = await _run_browser_and_save()
            engine_used = "browser"
        else:
            if template is not None:
                try:
                    result = await agg.replay(payment, template)
                    engine_used = "replay"
                except Exception as e:  # noqa: BLE001
                    log.warning("auto: replay raised (%s)", e)
                    result = None
                inconclusive = result is None or result.final_status in ("unknown", "timeout", "error", "")
                if inconclusive and req.fallback_browser:
                    log.info("auto: replay inconclusive -> falling back to browser")
                    result = await _run_browser_and_save()
                    engine_used = "browser"
                elif result is None:
                    raise HTTPException(502, "Replay failed and browser fallback disabled (fallback_browser=false)")
            else:
                result = await _run_browser_and_save()
                engine_used = "browser"
    finally:
        result = _settle(result, engine_used)
        await db.update_transaction(tx_id, result)

        # USSD envoyé : on REND LA MAIN tout de suite avec 'ussd_sent' et on
        # finalise (poll verify -> verdict -> notify) EN TÂCHE DE FOND. Le client
        # reçoit le verdict final via webhook + Socket.IO. (Aucune notif terminale
        # n'est émise maintenant : 'ussd_sent' n'est pas un statut terminal.)
        if result.poll_after_close:
            import asyncio
            asyncio.create_task(
                _finalize_in_background(agg, payment, result, tx_id, req,
                                        engine_used, app_id)
            )
        else:
            # Verdict déjà terminal (succès/échec avant USSD, panne) : on crédite
            # le solde si succès (idempotent), puis on notifie (no-op si non terminal).
            await _credit_on_success(app_id, tx_id, result, req.amount, req.aggregator)
            notify_settled(
                tx_id, result, transaction_ref=result.transaction_id,
                amount=req.amount, network=req.network, phone=req.phone,
                end_user_ref=req.end_user_ref,
                provider_transaction_id=result.provider_transaction_id,
                callback_url=req.callback_url,
            )

    if result.error_code in UPSTREAM_CODES:
        log.warning("Upstream indisponible (tx_id=%s, code=%s): %s",
                    tx_id, result.error_code, result.error or result.final_message)
        raise HTTPException(
            503,
            {"code": result.error_code, "message": UPSTREAM_MESSAGES[result.error_code]},
        )

    return _render_response(result, debug)


async def _execute_direct(cls, req, app_id, api_key_id, debug, app_name):
    """/pay pour un agrégateur « API directe » : initie, rend `ussd_sent`, puis
    finalise en tâche de fond (webhook ou polling) comme le chemin navigateur."""
    agg = cls(browser=None, llm=None, db=db)
    canonical_network = agg.normalize_network(req.network)
    if canonical_network is None:
        raise HTTPException(422, {
            "error": "invalid_network",
            "message": f"Réseau '{req.network}' non supporté par l'agrégateur '{req.aggregator}'.",
            "aggregator": req.aggregator,
            "supported_networks": agg.supported_networks,
        })
    label = f"MobileWallet {app_name}" if app_name else "MobileWallet"
    payment = PaymentRequest(
        amount=req.amount, phone=req.phone, network=canonical_network,
        email=req.email, sender_name=label, callback_url=req.callback_url, raison=label,
    )
    await _guard_duplicate(req)
    tx_id = await db.insert_pending(
        req.aggregator, "api", payment,
        app_id=app_id, api_key_id=api_key_id, end_user_ref=req.end_user_ref,
    )
    result = None
    try:
        result = await agg.pay_direct(payment, tx_id=tx_id)
    finally:
        result = _settle(result, "api")
        await db.update_transaction(tx_id, result)
        if result.poll_after_close:
            import asyncio
            asyncio.create_task(
                _finalize_in_background(agg, payment, result, tx_id, req, "api", app_id))
        else:
            notify_settled(
                tx_id, result, transaction_ref=result.transaction_id,
                amount=req.amount, network=req.network, phone=req.phone,
                end_user_ref=req.end_user_ref,
                provider_transaction_id=result.provider_transaction_id,
                callback_url=req.callback_url,
            )
    if result.error_code in UPSTREAM_CODES:
        raise HTTPException(
            503, {"code": result.error_code, "message": UPSTREAM_MESSAGES[result.error_code]})
    return _render_response(result, debug)


async def _finalize_in_background(agg, payment, result, tx_id, req, engine_used,
                                  app_id=None):
    """Phase 2 (hors requête /pay) : poll verify -> verdict -> persiste + notifie.

    Lancée quand l'USSD a été envoyé. Le poll tourne sans navigateur ni session ;
    le verdict final part ensuite au client via webhook (si callback_url fourni)
    et Socket.IO.
    """
    try:
        # Le replay et le navigateur ont chacun leur boucle poll (volontairement
        # distinctes). On route selon ce que le spec porte.
        spec = result.poll_after_close or {}
        if "cfg" in spec:
            await agg.finalize_after_close_replay(payment, result)
        else:
            await agg.finalize_after_close(payment, result)
        result = _settle(result, engine_used)
        await db.update_transaction(tx_id, result)
        await _credit_on_success(app_id, tx_id, result, req.amount, req.aggregator)
        notify_settled(
            tx_id, result, transaction_ref=result.transaction_id,
            amount=req.amount, network=req.network, phone=req.phone,
            end_user_ref=req.end_user_ref,
            provider_transaction_id=result.provider_transaction_id,
            callback_url=req.callback_url,
        )
    except Exception as e:  # noqa: BLE001
        log.exception("finalize_in_background tx=%s a échoué: %s", tx_id, e)


@router.post(
    "/pay",
    response_model=PayResponse,
    tags=["payments"],
    summary="Exécuter un paiement",
    responses={
        404: {"description": "Agrégateur inconnu."},
        400: {"description": "Mode invalide."},
        422: {"description": "Réseau non supporté (renvoie la liste exacte attendue)."},
        409: {"description": "Mode replay sans template (lancer mode=browser d'abord)."},
        502: {"description": "Replay échoué et fallback navigateur désactivé."},
        503: {"description": "Service de paiement amont temporairement indisponible "
                             "(code=network_unavailable). Réessayer plus tard."},
    },
)
async def pay(
    req: PayRequest,
    ctx: AuthContext = Depends(require_api_key),
    debug: bool = Query(False, include_in_schema=False),
):
    """Exécute un paiement via l'agrégateur et le mode demandés.

    Authentifié par clé API (`Authorization: Bearer <clé>`). La transaction est
    rattachée à l'app de la clé (isolation) ; `end_user_ref` est conservé tel quel.

    - **auto** (défaut) : replay d'abord ; bascule navigateur si non concluant
      (selon `fallback_browser`).
    - **browser** : flux IA complet ; déduit et persiste le template curl.
    - **replay** : rejoue via le template stocké (409 si absent).
    """
    return await _execute_payment(req, ctx.app_id, ctx.api_key_id, debug,
                                  app_name=ctx.app_name)
