"""Projets du developer (self-service) : gérer ses apps et clés API via JWT.

Toutes les routes sont scopées au developer authentifié (le developer_id vient du
token, jamais de l'URL/corps). Une app qui n'appartient pas au dev renvoie 404.
Réutilise la génération de clés (core/auth) et le CRUD (core/tenants) déjà en place.
"""

from fastapi import APIRouter, Depends, HTTPException

from core import auth, tenants
from core.dev_auth import DevContext, require_dev
from core.schemas.admin import ApiKeyCreate, ApiKeyCreated
from core.schemas.projects import AppCreateSelf

router = APIRouter(
    tags=["projects"],
    dependencies=[Depends(require_dev)],
    responses={401: {"description": "JWT manquant ou invalide."},
               503: {"description": "Supabase non configuré."}},
)


def _require_db(value):
    if value is None:
        raise HTTPException(503, "Supabase non configuré — projets indisponibles.")
    return value


async def _ensure_owned(app_id: int, ctx: DevContext) -> None:
    """404 si l'app n'appartient pas au developer authentifié (pas de fuite)."""
    if not await tenants.app_belongs_to(app_id, ctx.developer_id):
        raise HTTPException(404, f"Aucune app {app_id} pour ce compte.")


@router.post("/apps", summary="Créer une app (projet)")
async def create_app(body: AppCreateSelf, ctx: DevContext = Depends(require_dev)):
    """Crée une app pour VOTRE compte. Génère son webhook_secret HMAC (non exposé)."""
    secret = auth.generate_secret()
    return _require_db(await tenants.create_app(
        ctx.developer_id, body.name, None, secret))


@router.get("/apps", summary="Lister vos apps")
async def list_apps(ctx: DevContext = Depends(require_dev)):
    return {"apps": await tenants.list_apps(ctx.developer_id)}


@router.post("/apps/{app_id}/keys", response_model=ApiKeyCreated,
             summary="Générer une clé API (clé en clair montrée une seule fois)")
async def create_key(app_id: int, body: ApiKeyCreate, ctx: DevContext = Depends(require_dev)):
    await _ensure_owned(app_id, ctx)
    if body.env not in ("test", "live"):
        raise HTTPException(400, "env doit être 'test' ou 'live'.")
    full_key, key_prefix, key_hash, last_four = auth.generate_api_key(body.env)
    row = _require_db(await tenants.insert_api_key(
        app_id, body.env, key_prefix, key_hash, last_four))
    return ApiKeyCreated(id=row["id"], app_id=app_id, env=body.env,
                         api_key=full_key, key_prefix=key_prefix, last_four=last_four)


@router.get("/apps/{app_id}/keys", summary="Lister les clés d'une app (sans la clé en clair)")
async def list_keys(app_id: int, ctx: DevContext = Depends(require_dev)):
    await _ensure_owned(app_id, ctx)
    return {"keys": await tenants.list_api_keys(app_id)}


@router.post("/apps/{app_id}/keys/{key_id}/revoke", summary="Révoquer une clé API")
async def revoke_key(app_id: int, key_id: int, ctx: DevContext = Depends(require_dev)):
    await _ensure_owned(app_id, ctx)
    row = await tenants.revoke_api_key(key_id)
    if row is None:
        raise HTTPException(404, f"Aucune clé active avec l'id {key_id}.")
    if row.get("key_hash"):
        auth.invalidate_cache(row["key_hash"])
    return {"revoked": {"id": row["id"], "app_id": row.get("app_id"),
                        "env": row.get("env"), "is_active": row.get("is_active")}}
