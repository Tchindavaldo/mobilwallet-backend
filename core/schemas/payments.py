"""Modèles request/response du domaine paiements (/pay)."""

from pydantic import BaseModel, Field


class PayRequest(BaseModel):
    amount: int = Field(..., description="Montant en XAF.", examples=[25])
    phone: str = Field(..., description="Numéro Mobile Money (sans +237).", examples=["696080087"])
    network: str = Field(..., description="Réseau : Orangemoney ou MTN.", examples=["Orangemoney"])
    email: str = Field(..., description="Email du payeur.", examples=["client@example.com"])
    sender_name: str = Field("Rauvalia", description="Nom affiché de l'émetteur.")
    callback_url: str = Field("", description="URL de callback (défaut: celle de l'agrégateur).")
    aggregator: str = Field("digikuntz", description="Nom de l'agrégateur (cf. GET /aggregators).")
    mode: str = Field("auto", description="auto | browser | replay.", examples=["auto"])
    fallback_browser: bool = Field(
        True,
        description="En mode auto : basculer sur le navigateur si le replay est non concluant.",
    )
    end_user_ref: str | None = Field(
        None,
        description="Identifiant opaque de votre utilisateur final (ex. votre user_id). "
                    "Stocké tel quel pour vos rapprochements ; facultatif.",
    )

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "amount": 25, "phone": "696080087", "network": "Orangemoney",
                    "email": "client@example.com", "aggregator": "digikuntz",
                    "mode": "auto", "fallback_browser": True,
                }
            ]
        }
    }


class PayResponse(BaseModel):
    """Vue CLIENT (dev qui intègre l'API) — strictement l'essentiel métier.

    Tout le détail technique (charge_url, plaintext, curl, trace, tokens…) est
    réservé à l'admin (voir PayResponseDebug + ?debug=true). Ne JAMAIS ajouter de
    champ interne ici.
    """
    success: bool = Field(..., description="Vrai si le paiement a abouti.")
    status: str = Field("", description="successful | ussd_sent | failed | cancelled | pending.")
    message: str = Field("", description="Message clair pour l'utilisateur final.")
    transaction_id: str = Field("", description="Référence transaction de l'agrégateur.")
    code: str = Field("", description="Code machine en cas d'erreur (ex. network_unavailable, operator_unavailable). Vide si OK.")


class PayResponseDebug(PayResponse):
    """Vue ADMIN/DEBUG — la vue client + tout le détail technique.

    Renvoyée uniquement quand l'admin passe ?debug=true (paramètre non documenté
    dans Swagger). À terme: endpoint admin dédié protégé (cf.
    todo/migrer-vue-admin-endpoint-dedie.md).
    """
    error: str = Field("", description="Message d'erreur technique éventuel.")
    payment_status: str = ""
    turns: int = Field(0, description="Nombre de tours de l'agent IA (mode browser).")
    input_tokens: int = 0
    output_tokens: int = 0
    flutterwave_charge_url: str = ""
    flutterwave_charge_body: str = ""
    flutterwave_charge_response: str = ""
    curl_replay: str = Field("", description="Commande curl reproductible déduite.")
    plaintext_payload: str = ""
    public_key: str = ""
    verify_url: str = ""
    verify_request_body: str = ""
    verify_last_response: str = ""
    verify_curl: str = ""
    final_status: str = ""
    final_message: str = ""
    error_signals: dict = {}
    captured_requests: list[dict] = []
