"""Modèles request/response des endpoints admin (gestion multi-tenant)."""

from pydantic import BaseModel, Field


class DeveloperCreate(BaseModel):
    email: str = Field(..., description="Email du developer (unique).", examples=["dev@acme.com"])
    name: str | None = Field(None, description="Nom du developer / société.")


class AppCreate(BaseModel):
    developer_id: int = Field(..., description="Id du developer propriétaire.")
    name: str = Field(..., description="Nom de l'app.", examples=["Acme Checkout"])
    callback_url: str | None = Field(
        None, description="URL webhook serveur-à-serveur pour recevoir le verdict des paiements.")


class ApiKeyCreate(BaseModel):
    env: str = Field("test", description="Environnement de la clé : 'test' ou 'live'.",
                     examples=["test"])


class ApiKeyCreated(BaseModel):
    """Réponse à la création d'une clé : la clé en clair n'est montrée QU'ICI."""
    id: int
    app_id: int
    env: str
    api_key: str = Field(..., description="Clé complète — affichée une seule fois, à stocker maintenant.")
    key_prefix: str
    last_four: str
