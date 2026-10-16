"""Persistance de la réserve de frais de payout PAR APP et PAR RÉSEAU — mixin de
`Database` (core/db.py). Table app_reserve_ledger (migration 026), montants au
centime : réserve créditée au dépôt, frais exacts consommés au payout, part
libérée vers la plateforme lors d'un payout vers un autre réseau, et rendue si
le payout échoue.

Hérité par `Database` ; utilise `self.enabled`, `self._client`, `self._run`.
"""

import logging

log = logging.getLogger("ai_browser2")

_REFUND_REASON = {"payout_fee": "payout_refund", "release": "release_refund"}


class ReserveMixin:
    async def _insert_reserve_row(self, row: dict) -> None:
        """Insert idempotent sur (transaction_id, direction, network, reason)."""
        def _do():
            exists = (self._client.table("app_reserve_ledger").select("id")
                      .eq("transaction_id", row["transaction_id"])
                      .eq("direction", row["direction"]).eq("network", row["network"])
                      .eq("reason", row["reason"]).limit(1).execute())
            if not exists.data:
                self._client.table("app_reserve_ledger").insert(row).execute()
        await self._run(_do)

    async def credit_app_reserve(self, app_id: int, amount: float, *, transaction_id: int,
                                 aggregator: str, network: str,
                                 reason: str = "deposit_reserve", currency: str = "XAF") -> None:
        """Crédite la réserve de frais de l'app pour un réseau (idempotent)."""
        if not self.enabled or app_id is None or not amount or transaction_id is None:
            return
        await self._insert_reserve_row({
            "app_id": app_id, "aggregator": aggregator, "network": network or "",
            "direction": "credit", "amount": round(float(amount), 6), "currency": currency,
            "transaction_id": transaction_id, "reason": reason})

    async def reserve_payout_with_fee(self, app_id: int, aggregator: str, amount: int,
                                      network: str, rates: dict, transaction_id: int | None,
                                      currency: str = "XAF", dry_run: bool = False) -> dict | None:
        """RPC atomique (ou devis si dry_run) : {ok, balance, needed, fee_from_balance,
        fee_from_reserve, balance_after}. None si BD désactivée / erreur."""
        if not self.enabled or app_id is None or not amount:
            return None
        return await self._run(lambda: self._client.rpc("reserve_payout_with_fee", {
            "p_app_id": app_id, "p_aggregator": aggregator, "p_amount": int(amount),
            "p_network": network or "", "p_rates": rates or {},
            "p_transaction_id": transaction_id, "p_currency": currency,
            "p_dry_run": dry_run}).execute().data)

    async def refund_reserve_debits(self, app_id: int, transaction_id: int) -> None:
        """Rend chaque débit de réserve d'un payout échoué (idempotent)."""
        if not self.enabled or transaction_id is None:
            return
        res = await self._run(lambda: self._client.table("app_reserve_ledger")
                              .select("aggregator, network, amount, reason, currency")
                              .eq("transaction_id", transaction_id)
                              .eq("direction", "debit").execute())
        for r in (res.data if res else []) or []:
            await self.credit_app_reserve(
                app_id, float(r["amount"]), transaction_id=transaction_id,
                aggregator=r["aggregator"], network=r["network"],
                reason=_REFUND_REASON.get(r["reason"], "payout_refund"),
                currency=r.get("currency") or "XAF")

    async def app_main_debit(self, transaction_id: int) -> int:
        """Montant débité du solde de l'app par ce payout (pour le rembourser)."""
        if not self.enabled or transaction_id is None:
            return 0
        res = await self._run(lambda: self._client.table("app_ledger").select("amount")
                              .eq("transaction_id", transaction_id)
                              .eq("direction", "debit").execute())
        return sum(int(r["amount"]) for r in (res.data if res else []) or [])

    async def reserves(self, app_id: int | None = None) -> dict:
        """Réserves : {agg: {réseau: montant}} pour une app, ou Σ toutes apps."""
        if not self.enabled:
            return {}
        def _q():
            q = self._client.table("app_reserve_by_network").select("app_id, aggregator, network, balance")
            if app_id is not None:
                q = q.eq("app_id", app_id)
            return q.execute()
        res = await self._run(_q)
        out: dict = {}
        for r in (res.data if res else []) or []:
            nets = out.setdefault(r["aggregator"], {})
            nets[r["network"]] = round(nets.get(r["network"], 0) + float(r["balance"]), 6)
        return out

    async def reserve_released(self) -> dict:
        """Réserve libérée vers la plateforme, cumulée : {agg: montant}."""
        if not self.enabled:
            return {}
        res = await self._run(lambda: self._client.table("app_reserve_released_by_aggregator")
                              .select("aggregator, released").execute())
        return {r["aggregator"]: float(r["released"]) for r in (res.data if res else []) or []}

    async def platform_credited(self, reason: str) -> dict:
        """Σ crédits plateforme d'une raison donnée, par agrégateur."""
        if not self.enabled:
            return {}
        res = await self._run(lambda: self._client.table("platform_ledger")
                              .select("aggregator, amount").eq("reason", reason)
                              .eq("direction", "credit").execute())
        out: dict = {}
        for r in (res.data if res else []) or []:
            out[r["aggregator"]] = out.get(r["aggregator"], 0) + int(r["amount"])
        return out

    async def has_unsettled(self, aggregator: str) -> bool:
        """Une transaction de cet agrégateur est-elle encore en vol (pending /
        ussd_sent) ? Pendant ce temps le solde réel et la compta divergent
        normalement, donc on ne calcule pas de surplus."""
        if not self.enabled:
            return True
        res = await self._run(lambda: self._client.table("transactions").select("id")
                              .eq("aggregator", aggregator)
                              .in_("status", ["pending", "ussd_sent"]).limit(1).execute())
        return res is None or bool(res.data)
