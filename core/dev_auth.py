"""Auth du COMPTE developer (self-service) : mot de passe + JWT access/refresh.

Distinct de core/auth.py (qui authentifie les clés API d'APP pour /pay). Ici on
gère l'identité HUMAINE du dev : il s'inscrit/se connecte (email + mot de passe)
et reçoit un JWT d'accès court + un refresh token long. Le JWT sert à gérer ses
projets (apps/clés) ; le `developer_id` en est extrait (jamais passé en paramètre).
"""

import logging
import secrets
import time
from dataclasses import dataclass

import bcrypt
import jwt
from fastapi import Header, HTTPException

from core import tenants
from core.config import settings

log = logging.getLogger("ai_browser2")

_ALGO = "HS256"


@dataclass
class DevContext:
    """Identité d'un developer authentifié par JWT."""
    developer_id: int
    email: str


# --- Mot de passe -------------------------------------------------------------

def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, password_hash: str) -> bool:
    if not password_hash:
        return False
    try:
        return bcrypt.checkpw(password.encode(), password_hash.encode())
    except (ValueError, TypeError):
        return False


# --- Tokens -------------------------------------------------------------------

def make_access_token(developer_id: int) -> str:
    """JWT d'accès signé, expirant après jwt_access_ttl_s."""
    now = int(time.time())
    payload = {"sub": str(developer_id), "type": "access",
               "iat": now, "exp": now + settings.jwt_access_ttl_s}
    return jwt.encode(payload, settings.jwt_secret, algorithm=_ALGO)


def decode_access_token(token: str) -> int:
    """Renvoie le developer_id d'un access token valide (401 sinon)."""
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[_ALGO])
    except jwt.PyJWTError:
        raise HTTPException(401, {"error": "invalid_token",
                                  "message": "Token d'accès invalide ou expiré."})
    if payload.get("type") != "access":
        raise HTTPException(401, {"error": "invalid_token", "message": "Type de token inattendu."})
    try:
        return int(payload["sub"])
    except (KeyError, ValueError):
        raise HTTPException(401, {"error": "invalid_token", "message": "Token malformé."})


def make_refresh_token() -> tuple[str, str, int]:
    """Génère un refresh token opaque. Retourne (token, token_hash, expires_at_epoch).

    Le token (clair) est remis au client ; seul son hash est persisté.
    """
    token = secrets.token_urlsafe(48)
    expires_at = int(time.time()) + settings.jwt_refresh_ttl_s
    return token, tenants.hash_refresh(token), expires_at


# --- Dépendance FastAPI -------------------------------------------------------

def _parse_bearer(authorization: str | None) -> str:
    if not authorization:
        raise HTTPException(401, {"error": "missing_token",
                                  "message": "Header Authorization requis (Bearer <JWT>)."})
    parts = authorization.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
        raise HTTPException(401, {"error": "invalid_authorization",
                                  "message": "Format attendu : 'Authorization: Bearer <JWT>'."})
    return parts[1].strip()


async def require_dev(authorization: str = Header(None)) -> DevContext:
    """Authentifie le developer par son JWT d'accès. 401 si invalide ; 403 si
    le compte est désactivé."""
    token = _parse_bearer(authorization)
    developer_id = decode_access_token(token)

    dev = await tenants.get_developer(developer_id)
    if dev is None:
        raise HTTPException(401, {"error": "unknown_developer",
                                  "message": "Compte introuvable."})
    if not dev.get("is_active", True):
        raise HTTPException(403, {"error": "inactive_developer",
                                  "message": "Compte désactivé."})
    return DevContext(developer_id=developer_id, email=dev.get("email", ""))
