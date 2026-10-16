"""Imputation comptable des frais agrégateur (payin et payout), commune à tous
les agrégateurs : /pay, webhooks, /payout. Le calcul pur vit dans core/fees.py ;
ici on résout la config en BD et on écrit dans le ledger.
"""

import logging

from core.db import db
from core.fees import compute_fees, fee_network, fee_payer, prepays_payout_fee, provider_fee

log = logging.getLogger("ai_browser2")


async def _rate(aggregator: str, network: str, operation: str) -> float:
    net = fee_network(network)
    return await db.get_aggregator_fee_rate(aggregator, net, operation) if net else 0.0


async def credit_payin(app_id: int | None, tx_id: int | None, amount: int,
                       aggregator: str, network: str) -> None:
    """Split d'un payin réussi : frais agrégateur, commission MW, part de l'app.
    Idempotent (ledger unique par transaction/direction)."""
    if app_id is None or not tx_id:
        return
    agg_config = await db.get_aggregator_config(aggregator) if aggregator else None
    app_config = await db.get_app_commission(app_id)
    rate = await _rate(aggregator, network, "deposit")
    payout_rate = await _rate(aggregator, network, "payout")
    b = compute_fees(amount, agg_config, app_config, rate, payout_rate)
    log.info("payin split tx=%s: gross=%s provider_fee=%s (%s, payer=%s) payout_reserve=%s "
             "mw_comm=%s app=%s platform=%s", tx_id, b.gross, b.provider_fee, rate,
             fee_payer(agg_config), b.payout_fee_reserve, b.mw_commission,
             b.app_amount, b.platform_amount)
    if b.provider_fee:
        await db.set_provider_fee(tx_id, b.provider_fee)
    await db.credit_app(app_id, b.app_amount, transaction_id=tx_id, reason="payin_successful")
    if b.platform_amount > 0:
        await db.credit_platform(b.platform_amount, transaction_id=tx_id, reason="payin_fees")
    elif b.platform_amount < 0:
        await db.debit_platform(-b.platform_amount, transaction_id=tx_id)


async def payout_charges(aggregator: str, network: str, amount: int) -> dict:
    """Ce qu'un payout coûte en compta : {provider_fee, app_extra, platform_extra}.

    - app_extra : à ajouter au débit de l'app (fee_payer='app', non prépayé) ;
    - platform_extra : à débiter de la plateforme (fee_payer='platform', ou frais
      déjà prépayés au dépôt et mis en réserve sur le solde plateforme)."""
    agg_config = await db.get_aggregator_config(aggregator) if aggregator else None
    fee = provider_fee(amount, agg_config, await _rate(aggregator, network, "payout"))
    on_platform = fee_payer(agg_config) == "platform" or prepays_payout_fee(agg_config)
    return {"provider_fee": fee,
            "app_extra": 0 if on_platform else fee,
            "platform_extra": fee if on_platform else 0}


async def refund_payout(row: dict) -> None:
    """Rembourse un payout d'app échoué/annulé : montant + frais imputés à l'app,
    ou frais rendus à la plateforme. Idempotent (ledger)."""
    app_id, tx_id = row.get("app_id"), row.get("id")
    if app_id is None or not tx_id:
        return
    amount, fee = int(row.get("amount") or 0), int(row.get("provider_fee") or 0)
    agg_config = await db.get_aggregator_config(row.get("aggregator", ""))
    on_platform = fee_payer(agg_config) == "platform" or prepays_payout_fee(agg_config)
    await db.credit_app(app_id, amount + (0 if on_platform else fee),
                        transaction_id=tx_id, reason="payout_refund")
    if on_platform and fee:
        await db.credit_platform(fee, transaction_id=tx_id, reason="payout_refund")
