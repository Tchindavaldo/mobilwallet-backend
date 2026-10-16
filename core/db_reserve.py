"""Persistance de la réserve de frais de payout PAR APP — mixin de `Database`
(core/db.py). Table app_reserve_ledger (migration 026) : frais de retrait
prépayés au dépôt, consommés au payout, rendus si le payout échoue.

Hérité par `Database` ; utilise `self.enabled`, `self._client`, `self._run`,
`self._insert_ledger_row`.
"""

import logging

log = logging.getLogger("ai_browser2")


class ReserveMixin:
    async def credit_app_reserve(self, app_id: int, amount: int, *, transaction_id: int,
                                 aggregator: str, reason: str = "payout_fee_reserve",
                                 currency: str = "XAF") -> None:
        """Crédite la réserve de frais de l'app (idempotent par transaction)."""
        if not self.enabled or app_id is None or not amount or transaction_id is None:
            return
        await self._insert_ledger_row("app_reserve_ledger", {
            "app_id": app_id, "aggregator": aggregator, "direction": "credit",
            "amount": int(amount), "currency": currency,
            "transaction_id": transaction_id, "reason": reason})

    async def reserve_payout_with_fee(self, app_id: int, amount: int, fee: int,
                                      transaction_id: int, currency: str = "XAF") -> dict | None:
        """RPC atomique : montant sur le solde de l'app, frais sur sa réserve (le
        manque sur le solde). {ok, balance, balance_after, from_reserve, from_main}
        ou {ok: False, balance, needed}. None si BD désactivée / erreur."""
        if not self.enabled or app_id is None or not amount or transaction_id is None:
            return None
        return await self._run(lambda: self._client.rpc("reserve_payout_with_fee", {
            "p_app_id": app_id, "p_amount": int(amount), "p_fee": int(fee or 0),
            "p_transaction_id": transaction_id, "p_currency": currency}).execute().data)

    async def payout_debits(self, transaction_id: int) -> dict:
        """Ce qu'un payout a débité : {'main': n, 'reserve': n} (pour le rembourser)."""
        out = {"main": 0, "reserve": 0}
        if not self.enabled or transaction_id is None:
            return out
        for key, table in (("main", "app_ledger"), ("reserve", "app_reserve_ledger")):
            res = await self._run(lambda t=table: self._client.table(t).select("amount")
                                  .eq("transaction_id", transaction_id)
                                  .eq("direction", "debit").execute())
            out[key] = sum(int(r["amount"]) for r in (res.data if res else []) or [])
        return out

    async def reserves_by_aggregator(self, app_id: int | None = None) -> dict:
        """Réserves de frais : {agg: n} pour une app, ou Σ toutes apps si None."""
        if not self.enabled:
            return {}
        def _q():
            q = self._client.table("app_reserve_by_aggregator").select("app_id, aggregator, balance")
            if app_id is not None:
                q = q.eq("app_id", app_id)
            return q.execute()
        res = await self._run(_q)
        out: dict = {}
        for r in (res.data if res else []) or []:
            out[r["aggregator"]] = out.get(r["aggregator"], 0) + int(r["balance"])
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
