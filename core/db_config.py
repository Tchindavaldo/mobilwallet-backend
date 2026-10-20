"""Persistance configuration — mixin de `Database` (core/db.py) : réglages
à chaud (table settings, max_tabs) et templates curl de replay (`<agrégateur>_curl_templates`).

Hérité par `Database` ; utilise `self.enabled`, `self._client`, `self._run`.
"""

import asyncio
import dataclasses
import logging
from typing import Any

from core.base import CurlTemplate

log = logging.getLogger("ai_browser2")


class ConfigMixin:
    # --- app_settings (réglages clé/valeur modifiables via API) ---
    async def get_setting(self, key: str) -> str | None:
        """Return the stored value for `key`, or None (missing / DB disabled)."""
        if not self.enabled:
            return None

        def _select():
            res = (
                self._client.table("app_settings")
                .select("value")
                .eq("key", key)
                .limit(1)
                .execute()
            )
            return res.data[0]["value"] if res.data else None

        try:
            return await asyncio.to_thread(_select)
        except Exception as e:  # noqa: BLE001
            log.warning("get_setting(%s) failed: %s", key, e)
            return None

    async def set_setting(self, key: str, value: str) -> bool:
        """Upsert a setting; True on success. No-op (False) if DB disabled."""
        if not self.enabled:
            return False

        def _upsert():
            self._client.table("app_settings").upsert(
                {"key": key, "value": str(value)}, on_conflict="key"
            ).execute()
            return True

        try:
            return await asyncio.to_thread(_upsert)
        except Exception as e:  # noqa: BLE001
            log.warning("set_setting(%s) failed: %s", key, e)
            return False

    async def get_max_tabs(self, default: int) -> int:
        """Persisted max-tabs-per-browser, falling back to `default` (env)."""
        raw = await self.get_setting("max_tabs_per_browser")
        try:
            return int(raw) if raw is not None else default
        except (ValueError, TypeError):
            return default

    async def set_max_tabs(self, value: int) -> bool:
        """Persist the max-tabs-per-browser threshold."""
        return await self.set_setting("max_tabs_per_browser", str(int(value)))

    # --- curl templates (deduced by browser mode, reused by replay mode) ---
    # Une table PAR agrégateur, préfixée par son nom (règle CLAUDE.md) :
    # `<agrégateur>_curl_templates` (ex. digikuntz_curl_templates).
    @staticmethod
    def _tpl_table(aggregator: str) -> str:
        return f"{aggregator}_curl_templates"

    async def save_template(
        self, aggregator: str, template: CurlTemplate, force: bool = False
    ) -> dict | None:
        """Append a NEW active version ONLY if it differs from the current active
        one (or always, when force=True). Never overwrites: the previous version
        is deactivated and kept as history. Rows stay grouped per aggregator
        (one active each)."""
        if not self.enabled:
            return None
        payload = dataclasses.asdict(template)
        # db_id n'est qu'une référence runtime (cf. CurlTemplate) : jamais
        # persistée dans le jsonb (sinon elle fausserait la comparaison
        # "template identique" et polluerait les versions).
        payload.pop("db_id", None)

        def _save_if_changed():
            client = self._client
            # Current active template for this aggregator (if any).
            cur = (
                client.table(self._tpl_table(aggregator))
                .select("id, template")
                .eq("aggregator", aggregator)
                .eq("is_active", True)
                .limit(1)
                .execute()
            )
            if not force and cur.data and cur.data[0].get("template") == payload:
                return cur.data[0]  # identical -> nothing to add
            # Deactivate the old active version, then insert the new active one.
            # Le nouveau template est 'untested' tant qu'un replay ne l'a pas
            # validé (status posé ensuite par mark_template_status).
            if cur.data:
                client.table(self._tpl_table(aggregator)).update({"is_active": False}).eq(
                    "id", cur.data[0]["id"]
                ).execute()
            res = (
                client.table(self._tpl_table(aggregator))
                .insert({"aggregator": aggregator, "template": payload,
                         "is_active": True, "status": "untested"})
                .execute()
            )
            return res.data[0] if res.data else None

        return await self._run(_save_if_changed)

    async def mark_template_status(self, aggregator: str, template_id: int, status: str) -> None:
        """Marque la fiabilité d'un template ('working' | 'failed' | 'untested').

        Appelé par le replay : 'working' si le rejeu a abouti, 'failed' si le
        template est inexploitable (clé invalide/absente, chiffrement KO). Un
        'failed' n'est plus jamais re-sélectionné par load_template.
        """
        if not self.enabled or not template_id:
            return

        def _update():
            self._client.table(self._tpl_table(aggregator)).update(
                {"status": status}
            ).eq("id", template_id).execute()

        try:
            await self._run(_update)
        except Exception as e:  # noqa: BLE001
            log.warning("mark_template_status(%s, %s) failed: %s", template_id, status, e)

    async def load_template(self, aggregator: str) -> CurlTemplate | None:
        """Charge le MEILLEUR template rejouable pour cet agrégateur.

        Priorité : le dernier 'working' (a déjà servi un replay réussi), à
        défaut le dernier 'untested' (déduit, jamais rejoué). Un 'failed' n'est
        JAMAIS retourné. L'id BD est joint (db_id) pour que le replay puisse
        ensuite marquer ce template working/failed selon le résultat.
        """
        if not self.enabled:
            return None

        def _select():
            # On ne s'appuie plus sur is_active : on classe par fiabilité puis
            # récence, en excluant les 'failed'.
            res = (
                self._client.table(self._tpl_table(aggregator))
                .select("id, template, status")
                .eq("aggregator", aggregator)
                .neq("status", "failed")
                .order("created_at", desc=True)
                .execute()
            )
            rows = res.data or []
            # working d'abord (le plus récent), sinon untested le plus récent.
            for wanted in ("working", "untested"):
                for row in rows:
                    if row.get("status") == wanted:
                        return row
            return None

        try:
            row: dict[str, Any] | None = await asyncio.to_thread(_select)
            if row:
                data = row.get("template", {}) or {}
                fields = {f.name for f in dataclasses.fields(CurlTemplate)}
                tpl = CurlTemplate(**{k: v for k, v in data.items() if k in fields})
                tpl.db_id = row.get("id")
                log.info("load_template: id=%s status=%s (agg=%s)",
                         tpl.db_id, row.get("status"), aggregator)
                return tpl
        except Exception as e:  # noqa: BLE001
            log.warning("load_template failed: %s", e)
        return None
