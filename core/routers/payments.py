"""Route paiement : POST /pay (dispatch agrégateur, garde anti-doublon, settle)."""

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse

from core import registry, runtime
from core.auth import AuthContext, require_api_key
from core.base import PaymentRequest, PaymentResult
from core.notifications import notify_settled
from core.config import settings
from core.db import db
from core.error_tracking import build_errors
from core.schemas.payments import PayRequest, PayResponse, PayResponseDebug
from core.upstream_errors import OPERATOR_UNAVAILABLE, UPSTREAM_CODES, UPSTREAM_MESSAGES

log = logging.getLogger("ai_browser2")

router = APIRouter()


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
    browser = runtime.get_browser()
    llm = runtime.get_llm()
    if not browser or not llm:
        raise HTTPException(500, "Not initialized")

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

    # Validate the network against this aggregator's supported list. On failure,
    # echo back the exact accepted values (422).
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

    payment = PaymentRequest(
        amount=req.amount,
        phone=req.phone,
        network=canonical_network,
        email=req.email,
        sender_name=req.sender_name,
        callback_url=req.callback_url,
    )

    await _guard_duplicate(req)

    async def _run_browser_and_save():
        """Browser flow + persist the curl template deduced during the run.

        The runner builds res.curl_template while it still holds the (now
        isolated) browser session; we just persist it here.
        """
        res = await agg.pay_via_browser(payment, tx_id=tx_id)
        if res.curl_template:
            await db.save_template(req.aggregator, res.curl_template)
        return res

    # Resolve the template / 409 paths BEFORE creating the pending row, so a
    # rejected request never leaves a dangling 'pending' transaction.
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

    # Insert the audit row as 'pending' now; update it with the verdict at the end.
    # Rattaché à l'app/clé authentifiée (isolation) + end_user_ref du client.
    tx_id = await db.insert_pending(
        req.aggregator, req.mode, payment,
        app_id=ctx.app_id, api_key_id=ctx.api_key_id, end_user_ref=req.end_user_ref,
    )
    result = None
    engine_used = "replay"  # quel moteur a réellement produit le résultat
    try:
        if req.mode == "replay":
            result = await agg.replay(payment, template)
            engine_used = "replay"
        elif req.mode == "browser":
            result = await _run_browser_and_save()
            engine_used = "browser"
        else:  # auto: replay first, fall back to browser if inconclusive
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
        # Notifie l'app du verdict (webhook signé, non bloquant, idempotent).
        notify_settled(
            tx_id, result, transaction_ref=result.transaction_id,
            amount=req.amount, network=req.network, phone=req.phone,
            end_user_ref=req.end_user_ref,
            provider_transaction_id=result.provider_transaction_id,
        )

    # Panne amont (API agrégateur OU réseau opérateur) : on a tracé le détail en
    # BD + logs ci-dessus ; au dev intégrateur on renvoie un 503 propre avec un
    # code stable + message FR, SANS exposer l'URL interne ni la stacktrace. Même
    # forme de réponse quel que soit le moteur (browser/replay) et le type de panne.
    if result.error_code in UPSTREAM_CODES:
        log.warning("Upstream indisponible (tx_id=%s, code=%s): %s",
                    tx_id, result.error_code, result.error or result.final_message)
        raise HTTPException(
            503,
            {"code": result.error_code, "message": UPSTREAM_MESSAGES[result.error_code]},
        )

    return _render_response(result, debug)


async def _guard_duplicate(req: PayRequest) -> None:
    """Garde anti-doublon par numéro (vaut pour curl ET navigateur).

    - dernière transaction encore 'pending' → bloque : à confirmer ou annuler.
    - dernière 'cancelled'/'successful' dans la fenêtre opérateur (selon le réseau)
      → bloque avec le temps restant à attendre. Le reste est rechargeable.
    """
    last = await db.last_transaction_for_number(req.aggregator, req.phone)
    if not last:
        return
    status = (last.get("status") or "").lower()
    if status == "pending":
        raise HTTPException(
            409,
            {
                "error": "pending_exists",
                "message": (
                    f"Une transaction est déjà en cours (pending) sur le numéro "
                    f"{req.phone}. Veuillez la confirmer ou l'annuler."
                ),
                "aggregator": req.aggregator,
                "phone": req.phone,
                "transaction_id": last.get("id"),
            },
        )

    # cancelled : la fenêtre opérateur court depuis l'ENVOI de l'USSD
    # (ussd_sent_at) ; fallbacks cancelled_at puis created_at.
    if status == "cancelled":
        elapsed = runtime.seconds_since(
            last.get("ussd_sent_at")
            or last.get("cancelled_at")
            or last.get("created_at")
        )
        window = settings.retry_window_for(last.get("network", ""))
        if elapsed is not None and elapsed < window:
            remaining = int(window - elapsed)
            raise HTTPException(
                409,
                {
                    "error": "retry_too_soon",
                    "message": (
                        f"Une transaction récente est en cours de validation sur le "
                        f"numéro {req.phone}. Réessayez dans {runtime.fmt_duration(remaining)}."
                    ),
                    "aggregator": req.aggregator,
                    "phone": req.phone,
                    "last_status": status,
                    "retry_after_s": remaining,
                },
            )

    # successful : on bloque un NOUVEAU paiement pendant la fenêtre réseau,
    # comptée depuis la VALIDATION USSD (validated_at). Fallback created_at.
    if status == "successful":
        elapsed = runtime.seconds_since(last.get("validated_at") or last.get("created_at"))
        window = settings.retry_window_for(last.get("network", ""))
        if elapsed is not None and elapsed < window:
            remaining = int(window - elapsed)
            raise HTTPException(
                409,
                {
                    "error": "retry_too_soon",
                    "message": (
                        f"Vous avez récemment effectué un paiement sur le numéro "
                        f"{req.phone}. Veuillez réessayer dans {runtime.fmt_duration(remaining)}."
                    ),
                    "aggregator": req.aggregator,
                    "phone": req.phone,
                    "last_status": status,
                    "retry_after_s": remaining,
                },
            )


def _settle(result: PaymentResult | None, engine_used: str) -> PaymentResult:
    """Normalise le résultat avant persistance (toujours appelé dans le finally).

    Garantit qu'une ligne ne reste jamais 'pending', aligne le statut sur les
    pannes amont, pose validated_at sur tout succès, et dérive les erreurs.
    """
    if result is None:
        result = PaymentResult()
        result.final_status = "error"
        result.final_message = "Paiement interrompu (erreur serveur)."
    # Réseau opérateur dérangé au /charge AVANT tout USSD : on l'expose comme
    # operator_unavailable (la transaction n'a pas atteint l'utilisateur).
    if not result.error_code and result.final_status == "network_down":
        result.error_code = OPERATOR_UNAVAILABLE
    # Panne amont (API/opérateur) : la transaction n'a JAMAIS abouti côté
    # opérateur — on aligne le statut sur le code (la garde anti-doublon ne
    # bloque pas le numéro, rien de débité).
    if result.error_code in UPSTREAM_CODES:
        result.final_status = result.error_code
        result.final_message = UPSTREAM_MESSAGES[result.error_code]
    # Filet: tout 'successful' = USSD validé. On garantit validated_at posé pour
    # TOUS les chemins de succès (la garde anti-doublon en dépend).
    if result.final_status == "successful" and not result.validated_at:
        import time as _t
        result.validated_at = _t.time()
    result.errors = build_errors(result, engine_used)
    return result


def _render_response(result: PaymentResult, debug: bool):
    """Construit la réponse client (défaut) ou admin/debug (?debug=true)."""
    client_status = result.final_status or result.payment_status or ""
    client = PayResponse(
        success=result.success,
        status=client_status,
        message=result.final_message or result.error,
        transaction_id=result.transaction_id,
        code=result.error_code or "",
    )
    if not debug:
        return client

    # Vue ADMIN/DEBUG (?debug=true) : vue client + détail technique. JSONResponse
    # explicite pour contourner le filtrage response_model=PayResponse.
    full = PayResponseDebug(
        **client.model_dump(),
        error=result.error,
        payment_status=result.payment_status,
        turns=result.turns,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        flutterwave_charge_url=result.flutterwave_charge_url,
        flutterwave_charge_body=result.flutterwave_charge_body,
        flutterwave_charge_response=result.flutterwave_charge_response,
        curl_replay=result.curl_replay,
        plaintext_payload=result.plaintext_payload,
        public_key=result.public_key,
        verify_url=result.verify_url,
        verify_request_body=result.verify_request_body,
        verify_last_response=result.verify_last_response,
        verify_curl=result.verify_curl,
        final_status=result.final_status,
        final_message=result.final_message,
        error_signals=result.error_signals,
        captured_requests=result.captured_requests,
    )
    return JSONResponse(content=full.model_dump())
