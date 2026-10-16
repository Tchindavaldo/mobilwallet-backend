"""Persistance diagnostic — mixin de `Database` (core/db.py) : erreurs
détectées (transaction_errors) et trace tour-par-tour de l'IA (transaction_traces).

Hérité par `Database` ; utilise `self.enabled`, `self._client`, `self._run`.
"""

import asyncio
import logging

log = logging.getLogger("ai_browser2")


class DiagnosticsMixin:
    async def save_errors(self, tx_id: int, errors: list[dict] | None) -> None:
        """Insert one transaction_errors row per detailed error, linked to tx_id.

        Each error dict: {engine, source, category, message, detail, turn}.
        No-op if there are no errors (a successful transaction logs none).
        """
        if not self.enabled or tx_id is None or not errors:
            return
        rows = [
            {
                "transaction_id": tx_id,
                "engine": e.get("engine", ""),
                "source": e.get("source", ""),
                "category": e.get("category"),
                "message": e.get("message"),
                "detail": e.get("detail"),
                "turn": e.get("turn"),
            }
            for e in errors
        ]

        def _insert():
            self._client.table("transaction_errors").insert(rows).execute()

        await self._run(_insert)

    async def get_errors(self, transaction_id: int) -> list[dict]:
        """Return the detailed error rows for a transaction (newest first)."""
        if not self.enabled:
            return []

        def _select():
            res = (
                self._client.table("transaction_errors")
                .select("*")
                .eq("transaction_id", transaction_id)
                .order("created_at", desc=True)
                .execute()
            )
            return res.data or []

        try:
            return await asyncio.to_thread(_select)
        except Exception as e:  # noqa: BLE001
            log.warning("get_errors failed: %s", e)
            return []

    async def save_trace(self, tx_id: int, trace: list[dict]) -> None:
        """Insert one transaction_traces row per AI turn, linked to tx_id."""
        if not self.enabled or tx_id is None or not trace:
            return
        rows = [
            {
                "transaction_id": tx_id,
                "turn": e.get("turn"),
                "url": e.get("url"),
                "elements": e.get("elements"),
                "thought": e.get("thought"),
                "actions": e.get("actions"),
                "objective_reached": e.get("objective_reached", False),
                "error": e.get("error"),
            }
            for e in trace
        ]

        def _insert():
            self._client.table("transaction_traces").insert(rows).execute()

        await self._run(_insert)

    async def get_traces(self, transaction_id: int) -> list[dict]:
        """Return the per-turn trace rows for a transaction, ordered by turn."""
        if not self.enabled:
            return []

        def _select():
            res = (
                self._client.table("transaction_traces")
                .select("*")
                .eq("transaction_id", transaction_id)
                .order("turn")
                .execute()
            )
            return res.data or []

        try:
            return await asyncio.to_thread(_select)
        except Exception as e:  # noqa: BLE001
            log.warning("get_traces failed: %s", e)
            return []
