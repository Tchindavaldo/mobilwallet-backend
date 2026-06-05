"""Authentification multi-tenant : résolution des clés API + dépendances FastAPI.

Le dev intégrateur envoie UNIQUEMENT sa clé (`Authorization: Bearer sk_live_…`).
Le backend en calcule le hash et résout, via la vue Supabase `api_key_context`,
l'app + le developer + l'environnement. Un petit cache mémoire (TTL) évite de
taper la BD à chaque requête.

Deux dépendances :
  - require_api_key  -> AuthContext (routes tenant : /pay, historique…) ;
  - require_admin    -> garde les endpoints de gestion (X-Admin-Key).
"""

import hashlib
import hmac
import logging
import secrets
import time
from dataclasses import dataclass

from fastapi import Header, HTTPException

from core import tenants
from core.config import settings

log = logging.getLogger("ai_browser2")

_KEY_BYTES = 24  # entropie du corps de la clé (token_urlsafe -> ~32 chars)


@dataclass
class AuthContext:
    """Identité résolue d'un appelant authentifié par clé API."""
    api_key_id: int
    app_id: int
    developer_id: int
    env: str                 # 'test' | 'live'
    callback_url: str
    webhook_secret: str


# --- Génération / hachage des clés -------------------------------------------

def hash_key(full_key: str) -> str:
    """Hash stable d'une clé (sha256 hex). La clé a une haute entropie, donc
    sha256 indexable suffit (pas besoin de bcrypt) — approche Stripe-like."""
    return hashlib.sha256(full_key.encode()).hexdigest()


def generate_api_key(env: str) -> tuple[str, str, str, str]:
    """Génère une nouvelle clé pour l'environnement `env` ('test'|'live').

    Retourne (full_key, key_prefix, key_hash, last_four). `full_key` n'est
    montré qu'UNE fois à la création ; seul le hash/prefix est persisté.
    """
    body = secrets.token_urlsafe(_KEY_BYTES)
    full_key = f"sk_{env}_{body}"
    key_prefix = f"sk_{env}_{body[:6]}"
    return full_key, key_prefix, hash_key(full_key), full_key[-4:]


def generate_secret() -> str:
    """Secret HMAC d'une app (signature des webhooks sortants)."""
    return secrets.token_urlsafe(32)


# --- Cache mémoire des clés résolues -----------------------------------------

_cache: dict[str, tuple[AuthContext, float]] = {}


def _cache_get(key_hash: str) -> AuthContext | None:
    hit = _cache.get(key_hash)
    if hit is None:
        return None
    ctx, expiry = hit
    if time.monotonic() >= expiry:
        _cache.pop(key_hash, None)
        return None
    return ctx


def _cache_put(key_hash: str, ctx: AuthContext) -> None:
    _cache[key_hash] = (ctx, time.monotonic() + settings.api_key_cache_ttl_s)


def invalidate_cache(key_hash: str) -> None:
    """Purge une clé du cache (appelé à la révocation pour effet immédiat)."""
    _cache.pop(key_hash, None)


# --- Dépendances FastAPI ------------------------------------------------------

def _parse_bearer(authorization: str | None) -> str:
    """Extrait la clé d'un header `Authorization: Bearer <key>` (401 sinon)."""
    if not authorization:
        raise HTTPException(401, {"error": "missing_api_key",
                                  "message": "Header Authorization requis (Bearer <clé API>)."})
    parts = authorization.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
        raise HTTPException(401, {"error": "invalid_authorization",
                                  "message": "Format attendu : 'Authorization: Bearer <clé API>'."})
    return parts[1].strip()


async def require_api_key(authorization: str = Header(None)) -> AuthContext:
    """Authentifie l'appelant par sa clé API et renvoie son AuthContext.

    401 si la clé est absente/mal formée ou inconnue ; 403 si la clé, l'app ou
    le developer est désactivé.
    """
    full_key = _parse_bearer(authorization)
    key_hash = hash_key(full_key)

    ctx = _cache_get(key_hash)
    if ctx is not None:
        return ctx

    row = await tenants.resolve_api_key(key_hash)
    if row is None:
        raise HTTPException(401, {"error": "invalid_api_key",
                                  "message": "Clé API inconnue ou révoquée."})
    if not (row.get("key_active") and row.get("app_active") and row.get("dev_active")):
        raise HTTPException(403, {"error": "inactive_api_key",
                                  "message": "Clé API, app ou compte désactivé."})

    ctx = AuthContext(
        api_key_id=row["api_key_id"],
        app_id=row["app_id"],
        developer_id=row["developer_id"],
        env=row["env"],
        callback_url=row.get("callback_url") or "",
        webhook_secret=row.get("webhook_secret") or "",
    )
    _cache_put(key_hash, ctx)
    return ctx


async def require_admin(x_admin_key: str = Header(None)) -> None:
    """Garde les endpoints de gestion (developers/apps/clés). 401 si la clé admin
    est absente, non configurée côté serveur, ou ne correspond pas."""
    expected = settings.admin_api_key
    if not expected or not x_admin_key or not hmac.compare_digest(x_admin_key, expected):
        raise HTTPException(401, {"error": "admin_auth_required",
                                  "message": "Header X-Admin-Key invalide ou manquant."})
