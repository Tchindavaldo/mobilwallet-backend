"""Calcul des commissions sur un payin.

Flux (sur un montant brut envoyé par le client) :
  1. L'agrégateur (ex. DigiKUNTZ) prélève son taux → net agrégateur.
  2. MobileWallet prélève sa commission sur ce net → montant crédité à l'app.
  3. La plateforme MobileWallet est créditée de : frais agrégateur + commission MW.

Les taux sont lus depuis la table `aggregators` (migration 019). Si l'agrégateur
est inconnu ou que la BD est désactivée, on retombe sur 0% (aucune retenue).

Exemple (1 100 XAF, DigiKUNTZ 5%, MW 5%) :
  aggregator_fee = round(1100 * 0.05) = 55
  net_after_agg  = 1100 - 55 = 1045
  mw_commission  = round(1045 * 0.05) = 52
  app_amount     = 1045 - 52 = 993
  platform_amount = 55 + 52 = 107
"""

from __future__ import annotations

import math
from dataclasses import dataclass


# Fallback si l'agrégateur n'est pas en BD (ne bloque pas un payin).
_DEFAULTS = {
    "aggregator_fee_rate": 0.0,
    "mw_commission_type": "percent",
    "mw_commission_value": 0.0,
}


@dataclass
class FeeBreakdown:
    gross: int                # montant brut (ce que le client a payé)
    aggregator_fee: int       # frais prélevés par l'agrégateur
    net_after_aggregator: int # net après frais agrégateur
    mw_commission: int        # commission MobileWallet (sur le net)
    app_amount: int           # ce qui est crédité à l'app
    platform_amount: int      # ce qui est crédité à la plateforme MW


def compute_fees(gross: int, config: dict | None) -> FeeBreakdown:
    """Calcule la ventilation des frais pour un payin de `gross` XAF.

    `config` est la ligne de la table `aggregators` (ou None → 0%).
    Tous les montants sont arrondis à l'entier inférieur (pas de fraction de XAF).
    """
    cfg = config or _DEFAULTS

    agg_rate = float(cfg.get("aggregator_fee_rate") or 0)
    mw_type = cfg.get("mw_commission_type") or "percent"
    mw_value = float(cfg.get("mw_commission_value") or 0)

    aggregator_fee = math.floor(gross * agg_rate)
    net_after_agg = gross - aggregator_fee

    if mw_type == "flat":
        mw_commission = min(int(mw_value), net_after_agg)
    else:
        mw_commission = math.floor(net_after_agg * mw_value)

    app_amount = net_after_agg - mw_commission
    platform_amount = aggregator_fee + mw_commission

    return FeeBreakdown(
        gross=gross,
        aggregator_fee=aggregator_fee,
        net_after_aggregator=net_after_agg,
        mw_commission=mw_commission,
        app_amount=app_amount,
        platform_amount=platform_amount,
    )


def fees_info(gross: int, config: dict | None) -> dict:
    """Retourne la ventilation sous forme de dict (pour les endpoints /fees)."""
    b = compute_fees(gross, config)
    cfg = config or _DEFAULTS
    return {
        "gross": b.gross,
        "aggregator_fee_rate": float(cfg.get("aggregator_fee_rate") or 0),
        "aggregator_fee": b.aggregator_fee,
        "net_after_aggregator": b.net_after_aggregator,
        "mw_commission_type": cfg.get("mw_commission_type") or "percent",
        "mw_commission_value": float(cfg.get("mw_commission_value") or 0),
        "mw_commission": b.mw_commission,
        "app_amount": b.app_amount,
        "platform_amount": b.platform_amount,
    }
