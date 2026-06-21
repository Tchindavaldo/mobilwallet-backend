"""Modèles request/response des endpoints admin (gestion multi-tenant)."""

from pydantic import BaseModel, Field, field_validator
from core.schemas.payments import PayResponse
from core.schemas.payout import _normalize_cm_phone


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


class PlatformCredit(BaseModel):
    """Recharge du solde plateforme (argent propre de MobileWallet)."""
    amount: int = Field(..., description="Montant à ajouter au solde plateforme (XAF).",
                        examples=[100000])
    currency: str = Field("XAF", description="Devise.")


class AdminPayoutRequest(BaseModel):
    """Requête de virement (payout) lancée par un admin pour une app spécifique."""
    amount: int = Field(..., description="Montant à virer en XAF.", examples=[5000])
    account_bank_code: str = Field(..., description="Réseau du bénéficiaire — STRICT : "
                                   "exactement 'ORANGEMONEY' ou 'MTN'.", examples=["MTN"])
    account_number: str = Field(..., description="Numéro du compte bénéficiaire (local ou avec indicatif pays).",
                                examples=["237691224472"])
    receiver_name: str = Field(..., description="Nom du bénéficiaire.", examples=["John Doe"])
    currency: str = Field("XAF", description="Devise du virement.")
    narration: str = Field("", description="Motif / libellé du virement.")
    aggregator: str = Field("digikuntz", description="Nom de l'agrégateur.")
    callback_url: str = Field("", description="URL de callback (défaut: celle de l'app).")
    end_user_ref: str | None = Field(
        None, description="Identifiant opaque de l'utilisateur final ; facultatif.")

    @field_validator("account_number")
    @classmethod
    def normalize_account_number(cls, v: str) -> str:
        return _normalize_cm_phone(v)


class AggregatorConfigUpdate(BaseModel):
    """Mise à jour de la config de commission d'un agrégateur (admin)."""
    display_name: str | None = Field(None, description="Nom d'affichage.", examples=["DigiKUNTZ"])
    aggregator_fee_rate: float | None = Field(
        None, ge=0, lt=1,
        description="Taux prélevé par l'agrégateur sur le brut (ex. 0.05 = 5%).",
        examples=[0.05],
    )
    mw_commission_type: str | None = Field(
        None, description="Type de commission MobileWallet : 'percent' ou 'flat'.",
        examples=["percent"],
    )
    mw_commission_value: float | None = Field(
        None, ge=0,
        description="Valeur de la commission MW : taux (ex. 0.05) si type=percent, "
                    "montant fixe XAF si type=flat.",
        examples=[0.05],
    )
    active: bool | None = Field(None, description="Activer / désactiver l'agrégateur.")
