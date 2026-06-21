"""Modèles request/response du domaine payout (/payout — virements sortants)."""

from pydantic import BaseModel, Field, field_validator


def _normalize_cm_phone(v: str) -> str:
    """Normalise un numéro camerounais vers le format sans indicatif international.

    Entrées acceptées : "677087298", "+237677087298", "00237677087298", "237677087298"
    Sortie : "237677087298" (format attendu par DigiKUNTZ).
    """
    v = v.strip()
    if v.startswith("00237"):
        v = v[2:]   # "00237..." -> "237..."
    elif v.startswith("+237"):
        v = v[1:]   # "+237..." -> "237..."
    elif not v.startswith("237"):
        v = "237" + v
    return v


class PayoutRequest(BaseModel):
    amount: int = Field(..., description="Montant à virer en XAF.", examples=[5000])
    account_bank_code: str = Field(
        ..., description="Réseau du bénéficiaire — STRICT, exactement 'ORANGEMONEY' ou "
                         "'MTN' (aucune variante/conversion ; 422 sinon).",
        examples=["MTN"])
    account_number: str = Field(
        ..., description="Numéro du compte bénéficiaire (local ou avec indicatif pays).",
        examples=["237691224472"])

    @field_validator("account_number")
    @classmethod
    def normalize_account_number(cls, v: str) -> str:
        return _normalize_cm_phone(v)
    receiver_name: str = Field(..., description="Nom du bénéficiaire.", examples=["John Doe"])
    currency: str = Field("XAF", description="Devise du virement.", examples=["XAF"])
    narration: str = Field("", description="Motif / libellé du virement.",
                           examples=["Paiement fournisseur"])
    aggregator: str = Field("digikuntz", description="Nom de l'agrégateur (cf. GET /aggregators).")
    callback_url: str = Field("", description="URL de callback (défaut: celle de l'app).")
    end_user_ref: str | None = Field(
        None,
        description="Identifiant opaque de votre utilisateur final (ex. votre user_id). "
                    "Stocké tel quel pour vos rapprochements ; facultatif.",
    )

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "amount": 5000, "account_bank_code": "MTN",
                    "account_number": "237691224472", "receiver_name": "John Doe",
                    "currency": "XAF", "narration": "Paiement fournisseur",
                    "aggregator": "digikuntz",
                }
            ]
        }
    }


class PayoutResponse(BaseModel):
    """Vue CLIENT (dev qui intègre l'API) — l'essentiel métier.

    Le détail technique est réservé à l'admin (PayoutResponseDebug + ?debug=true).
    """
    success: bool = Field(..., description="Vrai si le virement a abouti.")
    status: str = Field("", description="successful | pending | failed | cancelled.")
    message: str = Field("", description="Message clair pour l'utilisateur final.")
    transaction_id: str = Field("", description="Référence transaction de l'agrégateur.")
    code: int = Field(200, description="Code HTTP de la réponse (200 si OK).")


class PayoutResponseDebug(PayoutResponse):
    """Vue ADMIN/DEBUG — la vue client + le détail technique (?debug=true)."""
    error: str = Field("", description="Message d'erreur technique éventuel.")
    provider_transaction_id: str = ""
    raw_status: str = ""
    balance_after: int | None = None
