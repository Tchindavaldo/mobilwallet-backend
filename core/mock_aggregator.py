"""Mode mock : simule les paiements sans appeler DigiKUNTZ.

Quand MOCK_PAYMENTS=true, /pay retourne des réponses fictives avec délais pour
simuler le workflow réel (ussd_sent → successful/cancelled après ~30-60s).
Socket.IO et webhook se déclenchent normalement après les délais.
"""

import asyncio
import logging
import random
from datetime import datetime, timezone

from core.base import PaymentRequest, PaymentResult

log = logging.getLogger("ai_browser2")


async def mock_pay_via_browser(
    payment: PaymentRequest,
    tx_id: int,
    scenario: str = "success",
) -> PaymentResult:
    """Simule un paiement : retourne ussd_sent immédiatement, puis successful/cancelled après délai.

    Args:
        payment: détails du paiement
        tx_id: ID interne de la transaction
        scenario: "success" (ussd_sent → successful) ou "cancel" (ussd_sent → cancelled)
    """
    log.info("MOCK: paiement simulé tx_id=%s amount=%s phone=%s scenario=%s",
             tx_id, payment.amount, payment.phone, scenario)

    # Génère un transaction_id fictif
    ts = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
    mock_tx_id = f"mock_{tx_id}_{ts}"

    # Phase 1 : USSD envoyé immédiatement (pas d'attente réseau)
    result = PaymentResult(
        final_status="ussd_sent",
        transaction_id=mock_tx_id,
        provider_transaction_id=mock_tx_id,
        success=False,
    )

    # Phase 2 : Attendre un délai avant le verdict final
    # Simule le temps d'attente réel (~30-60s en vrai, réduit pour les tests)
    delay_s = random.randint(1, 3)  # 1-3s en mode test (ajustable)
    log.info("MOCK: attente de %ds avant verdict final...", delay_s)
    await asyncio.sleep(delay_s)

    # Phase 3 : Verdict final
    if scenario == "cancel":
        result.final_status = "cancelled"
        result.success = False
        result.final_message = "MOCK: Paiement annulé par l'utilisateur"
    else:  # success
        result.final_status = "successful"
        result.success = True
        result.final_message = "MOCK: Paiement réussi"

    log.info("MOCK: verdict final tx_id=%s status=%s", tx_id, result.final_status)
    return result


async def mock_replay(
    payment: PaymentRequest,
    scenario: str = "success",
) -> PaymentResult:
    """Simule un replay curl : verdict immédiat ou après court délai."""
    ts = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
    mock_tx_id = f"mock_replay_{ts}"

    if scenario == "cancel":
        status = "cancelled"
        success = False
        message = "MOCK: Replay simulé → cancelled"
    else:
        status = "successful"
        success = True
        message = "MOCK: Replay simulé → successful"

    return PaymentResult(
        final_status=status,
        transaction_id=mock_tx_id,
        provider_transaction_id=mock_tx_id,
        success=success,
        final_message=message,
    )
