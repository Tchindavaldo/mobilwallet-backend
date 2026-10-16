"""Imputation comptable des frais agrégateur (payin et payout), commune à tous
les agrégateurs : /pay, webhooks, /payout. Le calcul pur vit dans core/fees.py ;
ici on résout la config en BD et on écrit dans le ledger.
"""

import logging

from core.db import db
from core.notifications import notify_settled
from core.fees import compute_fees, fee_network, fee_payer, provider_fee

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
    if b.payout_fee_reserve:
        await db.credit_app_reserve(app_id, b.payout_fee_reserve, transaction_id=tx_id,
                                    aggregator=aggregator, network=fee_network(network))
    if b.platform_amount > 0:
        await db.credit_platform(b.platform_amount, transaction_id=tx_id, reason="payin_fees")
    elif b.platform_amount < 0:
        await db.debit_platform(-b.platform_amount, transaction_id=tx_id)


async def payout_rates(aggregator: str) -> dict:
    """Taux de frais de payout par réseau : {'orange': 0.01, 'mtn': 0.013}."""
    return {n: await db.get_aggregator_fee_rate(aggregator, n, "payout") for n in ("orange", "mtn")}


async def payout_charges(aggregator: str, network: str, amount: int) -> dict:
    """Ce qu'un payout coûte en compta : {provider_fee, app_pays, platform_extra}.

    - app_pays : frais à la charge de l'app (fee_payer='app') -> réserve par
      réseau + solde, calculés atomiquement par db.reserve_payout_with_fee ;
    - platform_extra : frais à la charge de la plateforme (fee_payer='platform')."""
    agg_config = await db.get_aggregator_config(aggregator) if aggregator else None
    fee = provider_fee(amount, agg_config, await _rate(aggregator, network, "payout"))
    on_platform = fee_payer(agg_config) == "platform"
    return {"provider_fee": fee, "app_pays": not on_platform and fee > 0,
            "platform_extra": fee if on_platform else 0}


async def reserve_app_payout(app_id: int, aggregator: str, network: str, amount: int,
                             tx_id: int | None, currency: str = "XAF",
                             dry_run: bool = False) -> dict | None:
    """Devis (dry_run) ou réservation atomique d'un payout d'app : montant sur
    le solde, frais exacts sur la réserve du réseau, le reste des frais (fonds
    venus d'un autre réseau) arrondi à l'excès sur le solde."""
    charges = await payout_charges(aggregator, network, amount)
    rates = await payout_rates(aggregator) if charges["app_pays"] else {}
    return await db.reserve_payout_with_fee(app_id, aggregator, amount, fee_network(network),
                                            rates, tx_id, currency, dry_run)


async def refund_payout(row: dict) -> None:
    """Rembourse un payout d'app échoué/annulé : on rend exactement ce qui a été
    débité (solde + réserves), ou les frais à la plateforme s'ils étaient à sa
    charge. Idempotent (ledger)."""
    app_id, tx_id = row.get("app_id"), row.get("id")
    if app_id is None or not tx_id:
        return
    aggregator = row.get("aggregator", "")
    await db.credit_app(app_id, await db.app_main_debit(tx_id), transaction_id=tx_id,
                        reason="payout_refund")
    await db.refund_reserve_debits(app_id, tx_id)
    agg_config = await db.get_aggregator_config(aggregator)
    fee = int(row.get("provider_fee") or 0)
    if fee_payer(agg_config) == "platform" and fee:
        await db.credit_platform(fee, transaction_id=tx_id, reason="payout_refund")


async def settle_verdict(provider_id: str, internal: str, message: str,
                         settled_by: str = "webhook",
                         error_signals: dict | None = None) -> None:
    """MAJ BD + comptabilité + notification client pour un verdict terminal
    d'agrégateur (webhook, ou vérification manuelle d'un payout). Idempotent."""
    row = await db.get_transaction_by_provider_id(provider_id)
    if not row:
        return
    # message = texte UTILISATEUR ; error_signals = détail technique (code
    # agrégateur…) pour le dev, lisible via GET /status/{ref}.
    await db.update_status_by_provider_id(provider_id, internal, message=message,
                                          settled_by=settled_by,
                                          error_signals=error_signals)
    # Comptabilité (idempotente, via ledger) : un PAYIN réussi crédite l'app ;
    # un PAYOUT échoué/annulé rembourse le débit réservé (réussi : le débit reste).
    row_type = (row.get("type") or "payin").lower()
    app_id = row.get("app_id")
    if app_id is not None and row.get("id"):
        if row_type == "payin" and internal == "successful":
            await credit_payin(app_id, row["id"], row.get("amount", 0),
                               row.get("aggregator", ""), row.get("network", ""))
        elif row_type == "payout" and internal in ("failed", "cancelled"):
            await refund_payout(row)
    # Centimes d'arrondi accumulés -> plateforme (différé : compta écrite d'abord).
    from core import treasury
    treasury.schedule_sweep()
    # Idempotent : la réservation (tx, event) empêche un double envoi.
    notify_settled(
        row["id"], internal, transaction_ref=row.get("transaction_ref", ""),
        amount=row.get("amount", 0), network=row.get("network", ""),
        phone=row.get("phone", ""), end_user_ref=row.get("end_user_ref"),
        provider_transaction_id=provider_id,
        callback_url=row.get("callback_url") or "",
    )
