"""Modèles des opérations complémentaires : payouts groupés, page de paiement
hébergée, prédiction d'opérateur."""

from pydantic import BaseModel, Field

from core.schemas.payout import PayoutRequest


class BulkPayoutRequest(BaseModel):
    payouts: list[PayoutRequest] = Field(
        ..., min_length=1, max_length=20,
        description="1 à 20 virements, tous sur le même agrégateur.")


class PaymentPageRequest(BaseModel):
    amount: int = Field(..., description="Montant en XAF.", examples=[1000])
    return_url: str = Field(..., description="URL où le client revient après paiement.",
                            examples=["https://monsite.com/merci"])
    description: str = Field("", description="Libellé affiché au client (4-22 caractères).",
                             examples=["Commande 42"])
    phone: str = Field("", description="Numéro du payeur (facultatif : saisi sur la page sinon).")
    language: str = Field("FR", description="Langue de la page : FR ou EN.")
    aggregator: str = Field("pawapay", description="Agrégateur (doit proposer une page hébergée).")
    email: str = Field("", description="Email du payeur (facultatif).")
    callback_url: str = Field("", description="URL de callback (défaut : celle de l'app).")
    end_user_ref: str | None = Field(None, description="Identifiant opaque de votre utilisateur.")


class PredictNetworkRequest(BaseModel):
    phone: str = Field(..., description="Numéro dans n'importe quel format.",
                       examples=["696 08 00 87"])
    aggregator: str = Field("pawapay")
