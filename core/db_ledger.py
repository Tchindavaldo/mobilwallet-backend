"""Persistance comptable & payouts — mixin de `Database` (core/db.py).

Extrait de db.py pour garder chaque fichier sous le plafond de taille (CLAUDE.md).
Cette classe N'EST PAS instanciée seule : elle est héritée par `Database`, dont elle
utilise `self.enabled`, `self._client` (client Supabase) et `self._run` (exécuteur
borné par timeout). Trois domaines :

  - app_ledger : grand livre par app (credit payin / debit payout) + solde dérivé
    (vue app_balance) + réservation atomique du solde (RPC reserve_payout) + backfill.
    MobileWallet est lui-même un agrégateur : DigiKUNTZ n'a qu'un compte global, donc
    c'est nous qui tenons le solde de chaque app. Invariant : Σ soldes == global.
  - aggregators : config des taux par agrégateur (fee_rate, commission MW).
    Lecture BD avec fallback sur les valeurs par défaut (agrégateur inconnu = 0%).
  - payouts : insert/last/update des retraits (même table transactions, type='payout').
"""

import asyncio
import logging

log = logging.getLogger("ai_browser2")


class LedgerMixin:
    # --- helper : insert idempotent dans un ledger (check-then-insert) ---
    # PostgREST ne sait pas cibler un index UNIQUE PARTIEL (where transaction_id
    # is not null) via on_conflict ; on fait donc le check d'existence nous-mêmes
    # quand un transaction_id rattache la ligne (sinon insert simple).
    async def _insert_ledger_row(self, table: str, row: dict) -> None:
        if not self.enabled:
            return
        tx_id = row.get("transaction_id")
        direction = row.get("direction")

        def _do():
            client = self._client
            if tx_id is not None:
                exists = (client.table(table).select("id")
                          .eq("transaction_id", tx_id).eq("direction", direction)
                          .limit(1).execute())
                if exists.data:
                    return  # déjà présent -> idempotent, rien à faire
            client.table(table).insert(row).execute()

        await self._run(_do)

    # --- app_ledger (comptabilité par app : crédits payin / débits payout) ---
    async def credit_app(
        self, app_id: int, amount: int, *, transaction_id: int | None = None,
        currency: str = "XAF", reason: str = "",
    ) -> None:
        """Crédite le solde d'une app (payin réussi ou remboursement payout).

        IDEMPOTENT : la contrainte unique (transaction_id, direction) empêche un
        double crédit si polling ET webhook settlent le même payin. No-op si BD
        désactivée, app_id/amount manquant, ou (pour l'idempotence) transaction_id
        absent — on n'écrit JAMAIS un credit non rattaché à une transaction.
        """
        if not self.enabled or not app_id or not amount or transaction_id is None:
            return
        await self._insert_ledger_row("app_ledger", {
            "app_id": app_id, "direction": "credit", "amount": int(amount),
            "currency": currency, "transaction_id": transaction_id, "reason": reason,
        })

    async def credit_app_manual(self, app_id: int, amount: int, *,
                                currency: str = "XAF", reason: str = "manual") -> None:
        """Crédit MANUEL du solde d'une app (ajustement admin, sans transaction).

        Contrairement à credit_app, n'exige pas de transaction_id (pas de payin
        rattaché). ⚠️ crée de l'argent qui n'existe pas forcément chez DigiKUNTZ :
        peut introduire un écart visible dans la réconciliation. No-op si BD off."""
        if not self.enabled or not app_id or not amount:
            return
        await self._insert_ledger_row("app_ledger", {
            "app_id": app_id, "direction": "credit", "amount": int(amount),
            "currency": currency, "reason": reason,
        })

    async def reserve_payout(
        self, app_id: int, amount: int, transaction_id: int, currency: str = "XAF",
    ) -> dict | None:
        """Réserve (débite) atomiquement le solde pour un payout via la RPC Postgres.

        Retourne {ok, balance, balance_after} :
          - ok=True  -> débit inséré (solde suffisant) ;
          - ok=False -> rien débité (solde insuffisant), `balance` = disponible.
        None si BD désactivée / erreur (l'appelant traite None comme un refus sûr).
        """
        if not self.enabled or not app_id or not amount or transaction_id is None:
            return None

        def _rpc():
            res = self._client.rpc("reserve_payout", {
                "p_app_id": app_id, "p_amount": int(amount),
                "p_transaction_id": transaction_id, "p_currency": currency,
            }).execute()
            return res.data

        return await self._run(_rpc)

    async def get_app_balance(self, app_id: int) -> int:
        """Solde courant d'une app (vue app_balance). 0 si absent / BD désactivée."""
        if not self.enabled or not app_id:
            return 0

        def _select():
            res = (self._client.table("app_balance").select("balance")
                   .eq("app_id", app_id).limit(1).execute())
            return res.data[0]["balance"] if res.data else 0

        try:
            return int(await asyncio.to_thread(_select) or 0)
        except Exception as e:  # noqa: BLE001
            log.warning("get_app_balance failed: %s", e)
            return 0

    async def total_apps_balance(self) -> int:
        """Somme des soldes de TOUTES les apps (Σ credits − Σ debits).

        C'est le membre interne de l'invariant `Σ soldes apps == solde global
        DigiKUNTZ`. 0 si BD désactivée. Sert à la réconciliation admin."""
        if not self.enabled:
            return 0

        def _sum():
            rows = (self._client.table("app_ledger")
                    .select("direction, amount").execute()).data or []
            return sum((r["amount"] if r["direction"] == "credit" else -r["amount"])
                       for r in rows)

        try:
            return int(await asyncio.to_thread(_sum) or 0)
        except Exception as e:  # noqa: BLE001
            log.warning("total_apps_balance failed: %s", e)
            return 0

    # --- platform_ledger (argent propre de MobileWallet : marge/frais/flottant) ---
    async def credit_platform(self, amount: int, *, reason: str = "reload",
                              transaction_id: int | None = None,
                              currency: str = "XAF") -> None:
        """Recharge le solde plateforme (ou rembourse un payout plateforme échoué).

        Si transaction_id est fourni (remboursement), l'insertion est idempotente
        sur (transaction_id, direction). Une recharge manuelle n'a pas de
        transaction_id. No-op si BD désactivée / amount<=0."""
        if not self.enabled or not amount:
            return
        row = {"direction": "credit", "amount": int(amount), "currency": currency,
               "reason": reason}
        if transaction_id is not None:
            row["transaction_id"] = transaction_id
        # transaction_id None (recharge manuelle) -> insert simple (pas d'idempotence) ;
        # transaction_id présent (remboursement) -> idempotent via le helper.
        await self._insert_ledger_row("platform_ledger", row)

    async def debit_platform(self, amount: int, *, transaction_id: int,
                             reason: str = "payout", currency: str = "XAF") -> None:
        """Débite le solde plateforme pour un payout plateforme. SANS contrôle de
        solde (admin de confiance) : le solde peut devenir négatif. Idempotent sur
        (transaction_id, direction)."""
        if not self.enabled or not amount or transaction_id is None:
            return
        await self._insert_ledger_row("platform_ledger", {
            "direction": "debit", "amount": int(amount), "currency": currency,
            "transaction_id": transaction_id, "reason": reason})

    async def get_platform_balance(self) -> int:
        """Solde plateforme courant (vue platform_balance). 0 si BD désactivée.
        Peut être négatif (payouts plateforme non encore rechargés)."""
        if not self.enabled:
            return 0

        def _select():
            res = self._client.table("platform_balance").select("balance").limit(1).execute()
            return res.data[0]["balance"] if res.data else 0

        try:
            return int(await asyncio.to_thread(_select) or 0)
        except Exception as e:  # noqa: BLE001
            log.warning("get_platform_balance failed: %s", e)
            return 0

    async def backfill_ledger_from_transactions(self) -> int:
        """Crédite (idempotent) tous les payins 'successful' pas encore au ledger.

        À lancer une fois pour amorcer les soldes à partir de l'historique. Compte
        sur l'unique (transaction_id, direction) pour ne jamais re-créditer. Retourne
        le nombre de lignes crédit insérées (best-effort, 0 si BD désactivée)."""
        if not self.enabled:
            return 0

        def _backfill():
            client = self._client
            txs = (client.table("transactions")
                   .select("id, app_id, amount, currency")
                   .eq("type", "payin").eq("status", "successful")
                   .not_.is_("app_id", "null").execute()).data or []
            # Déjà crédités (on évite les doublons sans dépendre de l'upsert sur
            # index partiel, que PostgREST ne sait pas cibler).
            existing = (client.table("app_ledger").select("transaction_id")
                        .eq("direction", "credit")
                        .not_.is_("transaction_id", "null").execute()).data or []
            done = {e["transaction_id"] for e in existing}
            rows = [
                {"app_id": t["app_id"], "direction": "credit",
                 "amount": int(t["amount"]), "currency": t.get("currency") or "XAF",
                 "transaction_id": t["id"], "reason": "payin_successful"}
                for t in txs if t.get("amount") and t["id"] not in done
            ]
            if not rows:
                return 0
            client.table("app_ledger").insert(rows).execute()
            return len(rows)

        return await self._run(_backfill) or 0

    # --- aggregators (config des taux par agrégateur) ---

    async def get_aggregator_config(self, name: str) -> dict | None:
        """Retourne la config d'un agrégateur (taux, commission MW), ou None si absent."""
        if not self.enabled or not name:
            return None

        def _select():
            res = (self._client.table("aggregators").select("*")
                   .eq("name", name).limit(1).execute())
            return res.data[0] if res.data else None

        try:
            return await asyncio.to_thread(_select)
        except Exception as e:  # noqa: BLE001
            log.warning("get_aggregator_config(%s) failed: %s", name, e)
            return None

    async def list_aggregator_configs(self) -> list[dict]:
        """Retourne toutes les configs d'agrégateurs (actifs ou non)."""
        if not self.enabled:
            return []

        def _select():
            res = self._client.table("aggregators").select("*").execute()
            return res.data or []

        try:
            return await asyncio.to_thread(_select)
        except Exception as e:  # noqa: BLE001
            log.warning("list_aggregator_configs failed: %s", e)
            return []

    async def get_app_commission(self, app_id: int) -> dict | None:
        """Retourne la config commission MW de l'app (mw_commission_type/value).

        None si l'app n'a pas de surcharge (→ l'appelant tombera sur le défaut
        agrégateur). None aussi si BD désactivée ou app inconnue."""
        if not self.enabled or not app_id:
            return None

        def _select():
            res = (self._client.table("apps")
                   .select("mw_commission_type, mw_commission_value")
                   .eq("id", app_id).limit(1).execute())
            if not res.data:
                return None
            row = res.data[0]
            # NULL en BD = pas de surcharge
            if row.get("mw_commission_type") is None:
                return None
            return row

        try:
            return await asyncio.to_thread(_select)
        except Exception as e:  # noqa: BLE001
            log.warning("get_app_commission(%s) failed: %s", app_id, e)
            return None

    async def set_app_commission(
        self, app_id: int, *,
        mw_commission_type: str | None,
        mw_commission_value: float | None,
    ) -> bool:
        """Pose ou efface la commission spécifique d'une app.

        Passer type=None et value=None pour revenir au défaut agrégateur."""
        if not self.enabled or not app_id:
            return False

        patch = {
            "mw_commission_type": mw_commission_type,
            "mw_commission_value": float(mw_commission_value) if mw_commission_value is not None else None,
        }

        def _update():
            self._client.table("apps").update(patch).eq("id", app_id).execute()
            return True

        try:
            return await asyncio.to_thread(_update) or False
        except Exception as e:  # noqa: BLE001
            log.warning("set_app_commission(%s) failed: %s", app_id, e)
            return False

    async def upsert_aggregator_config(
        self, name: str, *, display_name: str | None = None,
        aggregator_fee_rate: float | None = None,
        mw_commission_type: str | None = None,
        mw_commission_value: float | None = None,
        active: bool | None = None,
    ) -> dict | None:
        """Crée ou met à jour la config d'un agrégateur. Retourne la ligne."""
        if not self.enabled:
            return None
        patch: dict = {"name": name}
        if display_name is not None:
            patch["display_name"] = display_name
        if aggregator_fee_rate is not None:
            patch["aggregator_fee_rate"] = float(aggregator_fee_rate)
        if mw_commission_type is not None:
            patch["mw_commission_type"] = mw_commission_type
        if mw_commission_value is not None:
            patch["mw_commission_value"] = float(mw_commission_value)
        if active is not None:
            patch["active"] = active

        def _upsert():
            res = (self._client.table("aggregators")
                   .upsert(patch, on_conflict="name").execute())
            return res.data[0] if res.data else None

        try:
            return await asyncio.to_thread(_upsert)
        except Exception as e:  # noqa: BLE001
            log.warning("upsert_aggregator_config(%s) failed: %s", name, e)
            return None

    # --- payouts (retraits : même table transactions, type='payout') ---
    async def insert_pending_payout(
        self, aggregator: str, req, *, app_id: int | None = None,
        api_key_id: int | None = None, end_user_ref: str | None = None,
    ) -> int | None:
        """Insère un payout 'pending' (type='payout') ; retourne son id.

        `req` est un PayoutRequest. account_number est aussi copié dans `phone`
        pour réutiliser les index/garde existants sur (aggregator, phone)."""
        if not self.enabled:
            return None
        row = {
            "aggregator": aggregator, "mode": "api", "type": "payout",
            "amount": req.amount, "phone": req.account_number,
            "network": req.account_bank_code, "email": "",
            "account_bank_code": req.account_bank_code,
            "account_number": req.account_number,
            "receiver_name": req.receiver_name, "currency": req.currency,
            "narration": req.narration, "status": "pending", "success": False,
        }
        if app_id is not None:
            row["app_id"] = app_id
        if api_key_id is not None:
            row["api_key_id"] = api_key_id
        if end_user_ref is not None:
            row["end_user_ref"] = end_user_ref

        def _insert():
            res = self._client.table("transactions").insert(row).execute()
            return res.data[0]["id"] if res.data else None

        try:
            return await asyncio.to_thread(_insert)
        except Exception as e:  # noqa: BLE001
            log.warning("insert_pending_payout failed: %s", e)
            return None

    async def last_payout_for_account(
        self, aggregator: str, account_number: str
    ) -> dict | None:
        """Dernier payout (type='payout') pour ce bénéficiaire, ou None."""
        if not self.enabled:
            return None

        def _select():
            res = (self._client.table("transactions").select("*")
                   .eq("aggregator", aggregator).eq("type", "payout")
                   .eq("account_number", account_number)
                   .order("created_at", desc=True).limit(1).execute())
            return res.data[0] if res.data else None

        try:
            return await asyncio.to_thread(_select)
        except Exception as e:  # noqa: BLE001
            log.warning("last_payout_for_account failed: %s", e)
            return None

    async def update_payout(
        self, tx_id: int, *, status: str, message: str = "", success: bool = False,
        provider_id: str = "", raw: str = "", settled_by: str = "",
    ) -> None:
        """MAJ le verdict d'un payout. Idempotent sur les colonnes simples."""
        if not self.enabled or tx_id is None:
            return
        patch = {"status": status, "message": message, "success": success}
        if provider_id:
            patch["provider_transaction_id"] = provider_id
        if settled_by:
            patch["settled_by"] = settled_by
        if raw:
            patch["charge_response"] = raw[:5000]
        from datetime import datetime, timezone
        if status == "successful":
            patch["validated_at"] = datetime.now(timezone.utc).isoformat()
        elif status == "cancelled":
            patch["cancelled_at"] = datetime.now(timezone.utc).isoformat()

        def _update():
            self._client.table("transactions").update(patch).eq("id", tx_id).execute()

        await self._run(_update)
