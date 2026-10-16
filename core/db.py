"""Supabase persistence layer (optional).

Stores a transaction audit row per payment attempt and the reusable per-aggregator
curl template. Degrades gracefully: if SUPABASE_URL/KEY are not set, `Database`
is disabled and all calls are safe no-ops so the server still runs locally.

Tables (see schema/supabase.sql):
  - transactions(id, aggregator, mode, amount, phone, network, email,
      transaction_ref, status, message, success, charge_response, error_signals,
      created_at)
  - curl_templates(id, aggregator, template jsonb, created_at, updated_at)
  - app_ledger(id, app_id, direction, amount, transaction_id, reason, created_at)
      + vue app_balance — comptabilité par app (cf. core/db_ledger.py).

Mixins hérités par Database (un fichier par domaine, sous le plafond de taille) :
  - core/db_ledger.py      : comptabilité par app/agrégateur, payouts ;
  - core/db_diagnostics.py : erreurs détectées et traces IA ;
  - core/db_config.py      : réglages à chaud et templates curl.
"""

import asyncio
import logging

from core.base import PaymentRequest, PaymentResult
from core.config import settings
from core.db_config import ConfigMixin
from core.db_diagnostics import DiagnosticsMixin
from core.db_ledger import LedgerMixin

log = logging.getLogger("ai_browser2")


class Database(LedgerMixin, DiagnosticsMixin, ConfigMixin):
    def __init__(self) -> None:
        self._client = None
        if settings.supabase_url and settings.supabase_key:
            try:
                from supabase import create_client

                self._client = create_client(settings.supabase_url, settings.supabase_key)
                log.info("Supabase connected")
            except Exception as e:  # noqa: BLE001 — never block startup on DB
                log.warning("Supabase init failed (%s); persistence disabled", e)
        else:
            log.info("Supabase not configured; persistence disabled")

    @property
    def enabled(self) -> bool:
        return self._client is not None

    async def _run(self, fn, timeout: float = 15.0):
        """Exécute un appel Supabase BLOQUANT dans un thread, BORNÉ par timeout.

        Le client Supabase est synchrone et n'a pas de timeout réseau : un hoquet
        peut pendre indéfiniment et bloquer la requête /pay APRÈS le verdict.
        On l'enveloppe donc dans asyncio.wait_for : au-delà du délai, on logge et
        on rend la main (la persistance est best-effort, jamais bloquante)."""
        try:
            return await asyncio.wait_for(asyncio.to_thread(fn), timeout=timeout)
        except asyncio.TimeoutError:
            log.warning("DB call timed out after %ss — skipping (best-effort)", timeout)
            return None
        except Exception as e:  # noqa: BLE001
            log.warning("DB call failed: %s", e)
            return None

    # --- transactions audit ---
    async def has_pending(self, aggregator: str, phone: str) -> bool:
        """True if a transaction for (aggregator, phone) is still 'pending'.

        Used to block launching a new payment on a number that already has one
        in flight. Safe (False) if Supabase is disabled.
        """
        if not self.enabled:
            return False

        def _select():
            res = (
                self._client.table("transactions")
                .select("id")
                .eq("aggregator", aggregator)
                .eq("phone", phone)
                .eq("status", "pending")
                .limit(1)
                .execute()
            )
            return bool(res.data)

        try:
            return await asyncio.to_thread(_select)
        except Exception as e:  # noqa: BLE001
            log.warning("has_pending failed: %s", e)
            return False

    async def last_transaction_for_number(
        self, aggregator: str, phone: str
    ) -> dict | None:
        """Most recent transaction for (aggregator, phone), or None.

        Returns the full row (id, status, created_at, ...) so the /pay guard can
        decide: a still-'pending' row blocks (confirm/cancel), and a recent
        non-success row within the retry window blocks with a wait time. Safe
        (None) if Supabase is disabled.
        """
        if not self.enabled:
            return None

        def _select():
            res = (
                self._client.table("transactions")
                .select("*")
                .eq("aggregator", aggregator)
                .eq("phone", phone)
                .order("created_at", desc=True)
                .limit(1)
                .execute()
            )
            return res.data[0] if res.data else None

        try:
            return await asyncio.to_thread(_select)
        except Exception as e:  # noqa: BLE001
            log.warning("last_transaction_for_number failed: %s", e)
            return None

    async def insert_pending(
        self, aggregator: str, mode: str, req: PaymentRequest,
        *, app_id: int | None = None, api_key_id: int | None = None,
        end_user_ref: str | None = None, callback_url: str | None = None,
    ) -> int | None:
        """Insert the transaction as 'pending' at the start; return its id.

        Les champs tenant (app_id/api_key_id/end_user_ref) rattachent la
        transaction à l'app authentifiée ; None quand l'appel n'est pas
        authentifié (ex. tests internes) — les colonnes sont nullable.
        """
        if not self.enabled:
            return None
        row = {
            "aggregator": aggregator,
            "mode": mode,
            "amount": req.amount,
            "phone": req.phone,
            "network": req.network,
            "email": req.email,
            "status": "pending",
            "success": False,
        }
        if app_id is not None:
            row["app_id"] = app_id
        if api_key_id is not None:
            row["api_key_id"] = api_key_id
        if end_user_ref is not None:
            row["end_user_ref"] = end_user_ref
        if callback_url:
            row["callback_url"] = callback_url

        def _insert():
            res = self._client.table("transactions").insert(row).execute()
            return res.data[0]["id"] if res.data else None

        try:
            return await asyncio.to_thread(_insert)
        except Exception as e:  # noqa: BLE001
            log.warning("insert_pending failed: %s", e)
            return None

    async def set_provider_id(self, tx_id: int, provider_id: str) -> None:
        """Enregistre l'id provider (DigiKUNTZ) DÈS qu'il est connu (création de
        transaction), pour que le webhook puisse retrouver cette ligne pendant le
        paiement (avant le settle final). No-op si BD désactivée / id vide."""
        if not self.enabled or tx_id is None or not provider_id:
            return

        def _update():
            (self._client.table("transactions")
                 .update({"provider_transaction_id": provider_id})
                 .eq("id", tx_id).execute())

        try:
            await asyncio.to_thread(_update)
        except Exception as e:  # noqa: BLE001
            log.warning("set_provider_id failed: %s", e)

    async def set_initiated(self, tx_id: int | None, provider_id: str, status: str,
                            message: str) -> None:
        """Après une initiation hors /pay (ex. page hébergée) : id agrégateur
        (= transaction_ref), statut et message. No-op si BD désactivée."""
        if not self.enabled or tx_id is None:
            return
        fields = {"provider_transaction_id": provider_id, "transaction_ref": provider_id,
                  "status": status, "message": message, "success": False}
        await self._run(lambda: self._client.table("transactions")
                        .update(fields).eq("id", tx_id).execute())

    async def fill_payer(self, provider_id: str, network: str, phone: str) -> None:
        """Complète réseau/numéro d'un dépôt créé sans (page hébergée), sans
        écraser une valeur déjà présente."""
        if not self.enabled or not provider_id:
            return
        for col, val in (("network", network), ("phone", phone)):
            if val:
                await self._run(lambda c=col, v=val: self._client.table("transactions")
                                .update({c: v}).eq("provider_transaction_id", provider_id)
                                .or_(f"{c}.is.null,{c}.eq.").execute())

    async def get_transaction_by_provider_id(self, provider_id: str) -> dict | None:
        """Retrouve une transaction par son id provider (pour le webhook)."""
        if not self.enabled or not provider_id:
            return None

        def _select():
            res = (self._client.table("transactions").select("*")
                   .eq("provider_transaction_id", provider_id).limit(1).execute())
            return res.data[0] if res.data else None

        try:
            return await asyncio.to_thread(_select)
        except Exception as e:  # noqa: BLE001
            log.warning("get_transaction_by_provider_id failed: %s", e)
            return None

    async def update_status_by_provider_id(
        self, provider_id: str, status: str, message: str = "",
        settled_by: str = "webhook",
    ) -> None:
        """MAJ le statut d'une transaction via son id provider (appelé par le
        webhook). Idempotent : n'écrase PAS un verdict terminal déjà posé."""
        if not self.enabled or not provider_id:
            return
        _TERMINAL = {"successful", "failed", "cancelled"}
        patch = {"status": status, "success": status == "successful",
                 "settled_by": settled_by}
        if message:
            patch["message"] = message
        # Mêmes timestamps que le settle polling (update_transaction), pour que la
        # garde anti-doublon ait le bon point de départ quand le WEBHOOK gagne la
        # course. cancelled_at = moment du verdict ; validated_at = validation USSD.
        # (ussd_sent_at n'est pas connu du webhook — il vient du /charge côté moteur.)
        from datetime import datetime, timezone
        now_iso = datetime.now(timezone.utc).isoformat()
        if status == "cancelled":
            patch["cancelled_at"] = now_iso
        elif status == "successful":
            patch["validated_at"] = now_iso

        def _update():
            q = (self._client.table("transactions")
                 .update(patch)
                 .eq("provider_transaction_id", provider_id))
            # Idempotence: ne pas réécrire une ligne déjà sur un statut terminal.
            q = q.not_.in_("status", list(_TERMINAL))
            q.execute()

        try:
            await asyncio.to_thread(_update)
        except Exception as e:  # noqa: BLE001
            log.warning("update_status_by_provider_id failed: %s", e)

    async def update_transaction(self, tx_id: int, result: PaymentResult) -> None:
        """Update a pending row with the final verdict once the payment settles."""
        if not self.enabled or tx_id is None:
            return
        final_status = result.final_status or result.payment_status or "unknown"
        patch = {
            "transaction_ref": result.transaction_id,
            "provider_transaction_id": result.provider_transaction_id or None,
            "status": final_status,
            "message": result.final_message,
            "success": result.success,
            "charge_response": result.provider_charge_response[:5000],
            "error_signals": result.error_signals,
        }
        # Trace QUI a settlé (polling/webhook). None pour un verdict hors course
        # (ex. échec avant USSD) -> on ne touche pas la colonne dans ce cas.
        if result.settled_by:
            patch["settled_by"] = result.settled_by
        from datetime import datetime, timezone
        # Instant de l'envoi USSD : base du calcul anti-doublon (la fenêtre
        # opérateur court depuis là). cf. migration 006_ussd_sent_at.
        if result.ussd_sent_at:
            patch["ussd_sent_at"] = datetime.fromtimestamp(
                result.ussd_sent_at, tz=timezone.utc).isoformat()
        # Horodate le passage à 'cancelled' (audit du moment du verdict).
        # cf. migration 005_cancelled_at.
        if final_status == "cancelled":
            patch["cancelled_at"] = datetime.now(timezone.utc).isoformat()
        # Instant de la validation USSD (paiement réussi) : base de la garde
        # anti-doublon après succès. cf. migration 007_validated_at.
        if result.validated_at:
            patch["validated_at"] = datetime.fromtimestamp(
                result.validated_at, tz=timezone.utc).isoformat()

        def _update():
            self._client.table("transactions").update(patch).eq("id", tx_id).execute()

        await self._run(_update)
        # Persist the per-turn AI trace into its dedicated table (browser mode).
        await self.save_trace(tx_id, result.trace)
        # Persist detailed errors (table transaction_errors).
        await self.save_errors(tx_id, result.errors)

    async def cancel_pending(self, tx_id: int, *, app_id: int | None = None) -> dict | None:
        """Force-settle a stuck 'pending' transaction to 'cancelled'.

        Only acts on rows still 'pending' (never overwrites a settled verdict).
        Si `app_id` est fourni, l'opération est bornée à cette app (isolation :
        un dev ne débloque que ses propres transactions).
        Returns the updated row, or None if not found / not pending / disabled.
        """
        if not self.enabled or tx_id is None:
            return None

        patch = {
            "status": "cancelled",
            "message": "Transaction annulée manuellement (déblocage d'un pending bloqué).",
            "success": False,
        }

        def _update():
            q = (
                self._client.table("transactions")
                .update(patch)
                .eq("id", tx_id)
                .eq("status", "pending")
            )
            if app_id is not None:
                q = q.eq("app_id", app_id)
            res = q.execute()
            return res.data[0] if res.data else None

        try:
            return await asyncio.to_thread(_update)
        except Exception as e:  # noqa: BLE001
            log.warning("cancel_pending failed: %s", e)
            return None

    async def list_transactions(self, aggregator: str | None = None, limit: int = 50) -> list[dict]:
        if not self.enabled:
            return []

        def _select():
            q = self._client.table("transactions").select("*")
            if aggregator:
                q = q.eq("aggregator", aggregator)
            res = q.order("created_at", desc=True).limit(limit).execute()
            return res.data or []

        try:
            return await asyncio.to_thread(_select)
        except Exception as e:  # noqa: BLE001
            log.warning("list_transactions failed: %s", e)
            return []

    async def get_transaction(self, transaction_ref: str) -> dict | None:
        if not self.enabled:
            return None

        def _select():
            res = (
                self._client.table("transactions")
                .select("*")
                .eq("transaction_ref", transaction_ref)
                .limit(1)
                .execute()
            )
            return res.data[0] if res.data else None

        try:
            return await asyncio.to_thread(_select)
        except Exception as e:  # noqa: BLE001
            log.warning("get_transaction failed: %s", e)
            return None

# Single shared instance.
db = Database()
