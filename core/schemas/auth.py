"""Modèles request/response de l'auth compte developer (self-service)."""

from pydantic import BaseModel, Field


class SignupRequest(BaseModel):
    email: str = Field(..., description="Email du developer (unique).", examples=["dev@acme.com"])
    password: str = Field(..., min_length=8, description="Mot de passe (8 caractères min).")
    name: str | None = Field(None, description="Nom du developer / société.")


class LoginRequest(BaseModel):
    email: str = Field(..., examples=["dev@acme.com"])
    password: str = Field(...)


class RefreshRequest(BaseModel):
    refresh_token: str = Field(..., description="Refresh token obtenu au login/signup.")


class TokenPair(BaseModel):
    """Couple de jetons renvoyé à l'inscription / connexion / rafraîchissement."""
    developer_id: int
    access_token: str = Field(..., description="JWT d'accès (court) — header Authorization: Bearer.")
    refresh_token: str = Field(..., description="Refresh token (long) pour renouveler l'accès.")
    token_type: str = "bearer"
