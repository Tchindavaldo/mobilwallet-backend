"""Détail figé du calcul sur les lignes de app_ledger (migration 027) + lecture
paginée du grand livre d'une app (GET /admin/apps/{id}/ledger). Mixin de Database."""

import logging

log = logging.getLogger("ai_browser2")


class LedgerDetailsMixin:
    async def set_ledger_details(self, transaction_id: int, direction: str,
                                 details: dict) -> None:
        """Pose le détail sur la/les ligne(s) app_ledger d'une transaction (ex. le
        débit d'un payout, écrit par la RPC SQL). Jamais bloquant."""
        if not self.enabled or transaction_id is None:
            return
        try:
            await self._run(lambda: self._client.table("app_ledger")
                            .update({k: v for k, v in details.items() if v is not None})
                            .eq("transaction_id", transaction_id)
                            .eq("direction", direction).execute())
        except Exception as e:  # noqa: BLE001
            log.warning("set_ledger_details(%s) failed: %s", transaction_id, e)

    async def list_app_ledger(self, app_id: int, *, page: int = 1, limit: int = 20,
                              aggregator: str | None = None, start_date: str | None = None,
                              end_date: str | None = None) -> dict | None:
        """Lignes du grand livre d'une app, plus récentes d'abord. None si BD off."""
        if not self.enabled:
            return None

        def _q():
            q = (self._client.table("app_ledger").select("*", count="exact")
                 .eq("app_id", app_id))
            if aggregator:
                q = q.eq("aggregator", aggregator)
            if start_date:
                q = q.gte("created_at", start_date)
            if end_date:
                q = q.lte("created_at", f"{end_date}T23:59:59.999999" if len(end_date) == 10
                          else end_date)
            off = (page - 1) * limit
            return q.order("created_at", desc=True).range(off, off + limit - 1).execute()

        res = await self._run(_q)
        total = (res.count if res else 0) or 0
        pages = (total + limit - 1) // limit if limit else 0
        return {"data": (res.data if res else []) or [],
                "pagination": {"current_page": page, "limit": limit, "total_pages": pages,
                               "total_items": total, "has_next_page": page < pages}}
