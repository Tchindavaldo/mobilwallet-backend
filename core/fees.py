"""Calcul de la commission MobileWallet sur un payin.

Flux réel :
  - DigiKUNTZ prend ses frais DIRECTEMENT sur le client via USSD (hors de notre compte).
  - Le compte global DigiKUNTZ reçoit le montant BRUT intégral.
  - MobileWallet prélève SA commission sur ce brut, puis crédite l'app du reste.

Exemple (1 100 XAF brut, commission MW = 5 XAF flat) :
  mw_commission = 5
  app_amount    = 1 095 XAF  → credit_app
  platform_amount = 5 XAF   → credit_platform

La commission est résolue dans cet ordre :
  1. Config spécifique à l'app (table apps.mw_commission_type/value) si non NULL
  2. Sinon config par défaut de l'agrégateur (table aggregators.mw_commission_type/value)
  3. Sinon 0 (aucune retenue)

Note : aggregator_fee_rate est stocké en BD à titre informatif pour les clients
(GET /aggregators/{name}/fees) mais n'entre PAS dans notre comptabilité.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass
class FeeBreakdown:
    gross: int            # montant brut (ce que le client a payé)
    mw_commission: int    # commission MobileWallet
    app_amount: int       # crédité à l'app
    platform_amount: int  # crédité à la plateforme MW


def _resolve_commission(app_config: dict | None, agg_config: dict | None) -> tuple[str, float]:
    """Retourne (type, value) de la commission MW à appliquer.

    Priorité : config app (si non NULL) > config agrégateur > 0.
    """
    if app_config and app_config.get("mw_commission_type") is not None:
        return (
            app_config["mw_commission_type"],
            float(app_config.get("mw_commission_value") or 0),
        )
    if agg_config and agg_config.get("mw_commission_type") is not None:
        return (
            agg_config["mw_commission_type"],
            float(agg_config.get("mw_commission_value") or 0),
        )
    return ("flat", 0.0)


def compute_fees(
    gross: int,
    agg_config: dict | None,
    app_config: dict | None = None,
) -> FeeBreakdown:
    """Calcule la ventilation MW pour un payin de `gross` XAF.

    `agg_config` : ligne de la table `aggregators` (défaut agrégateur).
    `app_config` : dict avec mw_commission_type/value issus de la table `apps`
                   (surcharge par app). None ou valeurs NULL = pas de surcharge.
    Montants arrondis à l'entier inférieur (pas de fraction de XAF).
    """
    comm_type, comm_value = _resolve_commission(app_config, agg_config)

    if comm_type == "flat":
        mw_commission = min(int(comm_value), gross)
    else:
        mw_commission = math.floor(gross * comm_value)

    app_amount = gross - mw_commission
    return FeeBreakdown(
        gross=gross,
        mw_commission=mw_commission,
        app_amount=app_amount,
        platform_amount=mw_commission,
    )


def fees_info(
    gross: int,
    agg_config: dict | None,
    app_config: dict | None = None,
) -> dict:
    """Ventilation sous forme de dict (pour les endpoints /fees)."""
    comm_type, comm_value = _resolve_commission(app_config, agg_config)
    b = compute_fees(gross, agg_config, app_config)
    return {
        "gross": b.gross,
        "aggregator_fee_rate": float((agg_config or {}).get("aggregator_fee_rate") or 0),
        "aggregator_fee_note": "Prélevé par l'agrégateur directement sur le client (hors comptabilité MW).",
        "mw_commission_type": comm_type,
        "mw_commission_value": comm_value,
        "mw_commission": b.mw_commission,
        "app_amount": b.app_amount,
        "platform_amount": b.platform_amount,
    }
