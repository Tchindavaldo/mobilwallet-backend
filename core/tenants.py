"""Persistance multi-tenant : developers / apps / api_keys + résolution & isolation.

Module séparé de core/db.py (qui gère les transactions/templates) pour respecter
la modularité — il réutilise le même client Supabase via l'instance `db`.
Toutes les écritures/lectures passent par `db._run(fn)` (thread borné par timeout),
et dégradent en no-op si Supabase n'est pas configuré.
"""

import logging

from core.db import db

log = logging.getLogger("ai_browser2")


def _client():
    """Client Supabase vivant, ou None si la persistance est désactivée."""
    return db._client if db.enabled else None


# --- Résolution d'une clé (lecture, chemin chaud) -----------------------------

async def resolve_api_key(key_hash: str) -> dict | None:
    """Résout une clé (par son hash) via la vue api_key_context : renvoie
    {api_key_id, env, app_id, developer_id, callback_url, webhook_secret,
    key_active, app_active, dev_active} ou None si inconnue / BD désactivée."""
    client = _client()
    if client is None or not key_hash:
        return None

    def _select():
        res = (client.table("api_key_context").select("*")
               .eq("key_hash", key_hash).limit(1).execute())
        return res.data[0] if res.data else None

    return await db._run(_select)


# --- CRUD admin (gestion) -----------------------------------------------------

async def create_developer(email: str, name: str | None) -> dict | None:
    client = _client()
    if client is None:
        return None

    def _insert():
        res = (client.table("developers")
               .insert({"email": email, "name": name}).execute())
        return res.data[0] if res.data else None

    return await db._run(_insert)


async def create_app(developer_id: int, name: str, callback_url: str | None,
                     webhook_secret: str) -> dict | None:
    client = _client()
    if client is None:
        return None

    def _insert():
        res = (client.table("apps").insert({
            "developer_id": developer_id, "name": name,
            "callback_url": callback_url, "webhook_secret": webhook_secret,
        }).execute())
        return res.data[0] if res.data else None

    return await db._run(_insert)


async def insert_api_key(app_id: int, env: str, key_prefix: str,
                         key_hash: str, last_four: str) -> dict | None:
    client = _client()
    if client is None:
        return None

    def _insert():
        res = (client.table("api_keys").insert({
            "app_id": app_id, "env": env, "key_prefix": key_prefix,
            "key_hash": key_hash, "last_four": last_four,
        }).execute())
        return res.data[0] if res.data else None

    return await db._run(_insert)


async def revoke_api_key(key_id: int) -> dict | None:
    """Désactive une clé (idempotent : n'agit que sur une clé encore active).
    Retourne la ligne révoquée, ou None si déjà inactive / introuvable."""
    client = _client()
    if client is None:
        return None

    def _update():
        res = (client.table("api_keys")
               .update({"is_active": False, "revoked_at": "now()"})
               .eq("id", key_id).eq("is_active", True).execute())
        return res.data[0] if res.data else None

    return await db._run(_update)


async def get_api_key(key_id: int) -> dict | None:
    """Lit une clé par id (sans le hash exposé inutilement)."""
    client = _client()
    if client is None:
        return None

    def _select():
        res = (client.table("api_keys")
               .select("id, app_id, env, key_prefix, last_four, is_active, revoked_at, created_at")
               .eq("id", key_id).limit(1).execute())
        return res.data[0] if res.data else None

    return await db._run(_select)


async def list_apps(developer_id: int) -> list[dict]:
    client = _client()
    if client is None:
        return []

    def _select():
        res = (client.table("apps")
               .select("id, developer_id, name, callback_url, is_active, created_at")
               .eq("developer_id", developer_id).order("created_at", desc=True).execute())
        return res.data or []

    return await db._run(_select) or []


async def list_api_keys(app_id: int) -> list[dict]:
    """Liste les clés d'une app SANS la clé en clair (jamais ré-exposée)."""
    client = _client()
    if client is None:
        return []

    def _select():
        res = (client.table("api_keys")
               .select("id, app_id, env, key_prefix, last_four, is_active, revoked_at, created_at")
               .eq("app_id", app_id).order("created_at", desc=True).execute())
        return res.data or []

    return await db._run(_select) or []


# --- Isolation des transactions par app --------------------------------------

async def list_transactions_for_app(app_id: int, limit: int = 50) -> list[dict]:
    """Transactions d'une app donnée (isolation : un dev ne voit que ses apps)."""
    client = _client()
    if client is None:
        return []

    def _select():
        res = (client.table("transactions").select("*")
               .eq("app_id", app_id)
               .order("created_at", desc=True).limit(limit).execute())
        return res.data or []

    return await db._run(_select) or []


async def get_transaction_scoped(transaction_ref: str, app_id: int) -> dict | None:
    """Transaction par référence, bornée à l'app appelante : None si elle
    n'existe pas OU appartient à une autre app (on ne révèle pas son existence)."""
    client = _client()
    if client is None:
        return None

    def _select():
        res = (client.table("transactions").select("*")
               .eq("transaction_ref", transaction_ref)
               .eq("app_id", app_id).limit(1).execute())
        return res.data[0] if res.data else None

    return await db._run(_select)
