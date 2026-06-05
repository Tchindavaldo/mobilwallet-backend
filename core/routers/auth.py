"""Auth compte developer (self-service) : signup / login / refresh / logout.

Inscription ouverte (compte actif immédiatement). Renvoie un JWT d'accès court +
un refresh token long. Le refresh est stocké hashé en BD (rotation au refresh,
révocation au logout).
"""

from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException

from core import dev_auth, tenants
from core.schemas.auth import LoginRequest, RefreshRequest, SignupRequest, TokenPair

router = APIRouter(tags=["auth"],
                   responses={503: {"description": "Supabase non configuré."}})


async def _issue_tokens(developer_id: int) -> TokenPair:
    """Émet un couple access+refresh et persiste le refresh (hashé)."""
    access = dev_auth.make_access_token(developer_id)
    refresh, refresh_hash, exp_epoch = dev_auth.make_refresh_token()
    exp_iso = datetime.fromtimestamp(exp_epoch, tz=timezone.utc).isoformat()
    await tenants.store_refresh(developer_id, refresh_hash, exp_iso)
    return TokenPair(developer_id=developer_id, access_token=access, refresh_token=refresh)


def _require_db(value):
    if value is None:
        raise HTTPException(503, "Supabase non configuré — comptes indisponibles.")
    return value


@router.post("/signup", response_model=TokenPair, summary="Créer un compte developer")
async def signup(body: SignupRequest):
    """Inscription ouverte : crée le compte (mot de passe hashé) et connecte le dev."""
    if await tenants.get_developer_by_email(body.email):
        raise HTTPException(409, {"error": "email_taken",
                                  "message": "Un compte existe déjà pour cet email."})
    pw_hash = dev_auth.hash_password(body.password)
    dev = _require_db(await tenants.create_developer_with_password(
        body.email, body.name, pw_hash))
    return await _issue_tokens(dev["id"])


@router.post("/login", response_model=TokenPair, summary="Connexion developer")
async def login(body: LoginRequest):
    dev = await tenants.get_developer_by_email(body.email)
    if not dev or not dev_auth.verify_password(body.password, dev.get("password_hash", "")):
        raise HTTPException(401, {"error": "invalid_credentials",
                                  "message": "Email ou mot de passe incorrect."})
    if not dev.get("is_active", True):
        raise HTTPException(403, {"error": "inactive_developer", "message": "Compte désactivé."})
    return await _issue_tokens(dev["id"])


@router.post("/refresh", response_model=TokenPair, summary="Renouveler les jetons")
async def refresh(body: RefreshRequest):
    """Échange un refresh token valide contre un nouveau couple (rotation :
    l'ancien refresh est révoqué)."""
    token_hash = tenants.hash_refresh(body.refresh_token)
    row = await tenants.get_refresh(token_hash)
    if row is None:
        raise HTTPException(401, {"error": "invalid_refresh",
                                  "message": "Refresh token invalide ou révoqué."})
    # Expiration : seconds_since(expires_at) > 0 signifie que la date est passée.
    from core.runtime import seconds_since
    age = seconds_since(row.get("expires_at"))
    if age is not None and age > 0:
        await tenants.revoke_refresh(token_hash)
        raise HTTPException(401, {"error": "expired_refresh", "message": "Refresh token expiré."})
    await tenants.revoke_refresh(token_hash)  # rotation : l'ancien ne sert plus
    return await _issue_tokens(row["developer_id"])


@router.post("/logout", summary="Déconnexion (révoque le refresh token)")
async def logout(body: RefreshRequest):
    await tenants.revoke_refresh(tenants.hash_refresh(body.refresh_token))
    return {"revoked": True}
