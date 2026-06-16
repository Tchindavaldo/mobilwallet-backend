"""Modèles request/response des endpoints admin (gestion multi-tenant)."""

from pydantic import BaseModel, Field
from core.schemas.payments import PayResponse


class DeveloperCreate(BaseModel):
    email: str = Field(..., description="Email du developer (unique).", examples=["dev@acme.com"])
    name: str | None = Field(None, description="Nom du developer / société.")


class AppCreate(BaseModel):
    developer_id: int = Field(..., description="Id du developer propriétaire.")
    name: str = Field(..., description="Nom de l'app.", examples=["Acme Checkout"])


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


class AdminPayRequest(BaseModel):
    """Requête de paiement lancée par un admin pour une app spécifique."""
    amount: int = Field(..., description="Montant en XAF.", examples=[25])
    phone: str = Field(..., description="Numéro Mobile Money (sans +237).", examples=["696080087"])
    network: str = Field(..., description="Réseau : Orangemoney ou MTN.", examples=["Orangemoney"])
    email: str = Field(..., description="Email du payeur.", examples=["client@example.com"])
    sender_name: str = Field("Rauvalia", description="Nom affiché de l'émetteur.")
    callback_url: str = Field("", description="URL de callback (défaut: celle de l'app).")
    aggregator: str = Field("digikuntz", description="Nom de l'agrégateur (cf. GET /aggregators).")
    mode: str = Field("auto", description="auto | browser | replay.", examples=["auto"])
    fallback_browser: bool = Field(
        True,
        description="En mode auto : basculer sur le navigateur si le replay est non concluant.",
    )
    end_user_ref: str | None = Field(
        None,
        description="Identifiant opaque de l'utilisateur final (ex. votre user_id). "
                    "Stocké tel quel pour vos rapprochements ; facultatif.",
    )
