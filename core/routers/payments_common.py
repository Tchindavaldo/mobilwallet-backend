"""Helpers partagés du domaine paiement (/pay, mock, admin) : garde anti-doublon,
normalisation du résultat, split comptable d'un payin réussi, rendu de réponse."""

import logging

from fastapi import HTTPException
from fastapi.responses import JSONResponse

from core import runtime
from core.base import PaymentResult
from core.config import settings
from core.db import db
from core.error_tracking import build_errors
from core.fees import compute_fees
from core.schemas.payments import PayRequest, PayResponse, PayResponseDebug
from core.upstream_errors import OPERATOR_UNAVAILABLE, UPSTREAM_CODES, UPSTREAM_MESSAGES

log = logging.getLogger("ai_browser2")


async def _credit_on_success(
    app_id: int | None, tx_id: int | None,
    result: PaymentResult, amount: int, aggregator: str = "",
) -> None:
    """Split comptable d'un payin réussi : crédite l'app (net) et la plateforme (commission).

    Flux : brut → commission MW (défaut agrégateur ou surcharge app) → app + plateforme.
    Les frais DigiKUNTZ sont prélevés directement sur le client via USSD (hors comptabilité).
    Idempotent (unique transaction_id/direction en BD). No-op si pas de succès,
    pas d'app, ou pas de tx_id."""
    if not (app_id and tx_id and result.final_status == "successful"):
        return
    agg_config = await db.get_aggregator_config(aggregator) if aggregator else None
    app_config = await db.get_app_commission(app_id) if app_id else None
    breakdown = compute_fees(amount, agg_config, app_config)
    log.info(
        "payin split tx=%s: gross=%s mw_comm=%s app=%s platform=%s (agg=%s app_override=%s)",
        tx_id, breakdown.gross, breakdown.mw_commission,
        breakdown.app_amount, breakdown.platform_amount,
        aggregator, app_config is not None,
    )
    await db.credit_app(app_id, breakdown.app_amount,
                        transaction_id=tx_id, reason="payin_successful")
    if breakdown.platform_amount > 0:
        await db.credit_platform(breakdown.platform_amount,
                                 transaction_id=tx_id, reason="payin_fees")


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
                        f"Une transaction récente sur le numéro {req.phone} n'a pas "
                        f"abouti (annulée). Réessayez dans {runtime.fmt_duration(remaining)}."
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
        code=200,  # 503 (panne amont) est levé via HTTPException avant ici.
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
        provider_charge_url=result.provider_charge_url,
        provider_charge_body=result.provider_charge_body,
        provider_charge_response=result.provider_charge_response,
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
