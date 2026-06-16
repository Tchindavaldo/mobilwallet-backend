"""Endpoints admin : gestion des developers / apps / clés API + execution paiements.

Tous protégés par `require_admin` (header X-Admin-Key). C'est ici qu'on crée les
comptes intégrateurs, qu'on génère/révoque leurs clés, et qu'on peut lancer des
paiements pour une app. La clé en clair n'est renvoyée qu'à sa création (jamais
ré-exposée ensuite).
"""

from fastapi import APIRouter, Depends, HTTPException, Query

from core import auth, tenants
from core.auth import require_admin
from core.schemas.admin import AdminPayRequest, ApiKeyCreate, ApiKeyCreated, AppCreate, DeveloperCreate
from core.schemas.payments import PayResponse
from core.routers.payments import _execute_payment

router = APIRouter(
    prefix="/admin",
    tags=["admin"],
    dependencies=[Depends(require_admin)],
    responses={401: {"description": "X-Admin-Key invalide ou manquant."},
               503: {"description": "Supabase non configuré."}},
)


def _require_db(row):
    """503 si la persistance est désactivée (les CRUD renvoient None)."""
    if row is None:
        raise HTTPException(503, "Supabase non configuré — gestion multi-tenant indisponible.")
    return row


@router.post("/developers", summary="Créer un developer (compte intégrateur)")
async def create_developer(body: DeveloperCreate):
    return _require_db(await tenants.create_developer(body.email, body.name))


@router.post("/apps", summary="Créer une app pour un developer")
async def create_app(body: AppCreate):
    """Génère le `webhook_secret` HMAC de l'app (utilisé pour signer les webhooks).
    Le secret n'est pas renvoyé ; il sert côté serveur à la signature."""
    secret = auth.generate_secret()
    app = _require_db(await tenants.create_app(
        body.developer_id, body.name, None, secret))
    return app


@router.get("/developers/{developer_id}/apps", summary="Lister les apps d'un developer")
async def list_apps(developer_id: int):
    return {"apps": await tenants.list_apps(developer_id)}


@router.post("/apps/{app_id}/keys", response_model=ApiKeyCreated,
             summary="Générer une clé API pour une app (clé en clair montrée 1 fois)")
async def create_key(app_id: int, body: ApiKeyCreate):
    if body.env not in ("test", "live"):
        raise HTTPException(400, "env doit être 'test' ou 'live'.")
    full_key, key_prefix, key_hash, last_four = auth.generate_api_key(body.env)
    row = _require_db(await tenants.insert_api_key(
        app_id, body.env, key_prefix, key_hash, last_four))
    return ApiKeyCreated(
        id=row["id"], app_id=app_id, env=body.env,
        api_key=full_key, key_prefix=key_prefix, last_four=last_four,
    )


@router.get("/apps/{app_id}/keys", summary="Lister les clés d'une app (sans la clé en clair)")
async def list_keys(app_id: int):
    return {"keys": await tenants.list_api_keys(app_id)}


@router.post("/apps/{app_id}/keys/{key_id}/revoke", summary="Révoquer une clé API")
async def revoke_key(app_id: int, key_id: int):
    """Désactive la clé (effet immédiat : purge aussi le cache de résolution)."""
    row = await tenants.revoke_api_key(key_id)
    if row is None:
        raise HTTPException(404, f"Aucune clé active avec l'id {key_id}.")
    if row.get("key_hash"):
        auth.invalidate_cache(row["key_hash"])
    return {"revoked": {"id": row["id"], "app_id": row.get("app_id"),
                        "env": row.get("env"), "is_active": row.get("is_active")}}


@router.post(
    "/apps/{app_id}/pay",
    response_model=PayResponse,
    summary="Lancer un paiement pour une app (sans clé API)",
    responses={
        404: {"description": "Agrégateur inconnu ou app introuvable."},
        400: {"description": "Mode invalide."},
        422: {"description": "Réseau non supporté."},
        409: {"description": "Doublon ou mode replay sans template."},
        502: {"description": "Replay échoué et fallback navigateur désactivé."},
        503: {"description": "Service de paiement amont temporairement indisponible."},
    },
)
async def admin_pay(
    app_id: int,
    body: AdminPayRequest,
    debug: bool = Query(False, include_in_schema=False),
):
    """Exécute un paiement pour l'app `app_id` sans passer par une clé API.

    La transaction est rattachée à l'app (isolation) avec `api_key_id=None`
    pour traçabilité (lancé par admin). Même logique que `POST /pay`.
    """
    app = await tenants.get_app(app_id)
    if app is None:
        raise HTTPException(404, {"error": "app_not_found",
                                  "message": f"Aucune app trouvée avec l'id {app_id}."})

    from core.schemas.payments import PayRequest
    req = PayRequest(
        amount=body.amount,
        phone=body.phone,
        network=body.network,
        email=body.email,
        sender_name=body.sender_name,
        callback_url=body.callback_url or app.get("callback_url") or "",
        aggregator=body.aggregator,
        mode=body.mode,
        fallback_browser=body.fallback_browser,
        end_user_ref=body.end_user_ref,
    )
    return await _execute_payment(req, app_id=app_id, api_key_id=None, debug=debug)
