"""Calcul de la ventilation d'un payin : frais agrégateur, commission MobileWallet,
part de l'app.

Qui paie les frais de l'agrégateur (colonne aggregators.fee_payer) :
  - 'customer' : l'agrégateur les prend DIRECTEMENT au client final (ex. DigiKUNTZ
    via l'USSD). Le compte marchand reçoit le BRUT intégral : frais hors compta.
  - 'app'      : l'agrégateur les prend sur le wallet marchand (ex. pawaPay). On
    les déduit du crédit de l'app : app = brut - frais - commission MW.
  - 'platform' : idem, mais MobileWallet les supporte : plateforme = commission - frais
    (peut être négatif).
Invariant : app + plateforme = brut - frais (= ce que le wallet a réellement reçu).
Frais = ceil(brut x taux) : arrondi au XAF supérieur, jamais de sur-crédit.

Exemple (1 100 XAF brut, commission MW = 5 XAF flat) :
  mw_commission = 5
  app_amount    = 1 095 XAF  → credit_app
  platform_amount = 5 XAF   → credit_platform

La commission est résolue dans cet ordre :
  1. Config spécifique à l'app (table apps.mw_commission_type/value) si non NULL
  2. Sinon config par défaut de l'agrégateur (table aggregators.mw_commission_type/value)
  3. Sinon 0 (aucune retenue)

Le taux de frais vient de la table aggregator_fee_rates (par réseau et opération),
résolu par l'appelant (core/settlement.py) et passé à compute_fees.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass
class FeeBreakdown:
    gross: int            # montant brut (ce que le client a payé)
    provider_fee: int     # frais agrégateur pris sur le wallet marchand (0 si 'customer')
    payout_fee_reserve: int  # frais de RETRAIT prépayés au dépôt (réserve de l'app)
    mw_commission: int    # commission MobileWallet
    app_amount: int       # crédité à l'app
    platform_amount: int  # crédité à la plateforme MW (négatif si elle supporte les frais)


FEE_PAYERS = ("customer", "app", "platform")


def fee_payer(agg_config: dict | None) -> str:
    """Qui paie les frais de l'agrégateur ('customer' par défaut)."""
    payer = (agg_config or {}).get("fee_payer") or "customer"
    return payer if payer in FEE_PAYERS else "customer"


def fee_network(network: str | None) -> str:
    """Réseau normalisé pour aggregator_fee_rates : 'mtn' | 'orange' | ''."""
    n = (network or "").lower()
    if n.startswith("orange"):
        return "orange"
    if n.startswith("mtn"):
        return "mtn"
    return ""


def prepays_payout_fee(agg_config: dict | None) -> bool:
    """Le client prépaie-t-il au dépôt les frais de retrait (payer='app' seulement) ?"""
    return fee_payer(agg_config) == "app" and bool((agg_config or {}).get("prepay_payout_fee"))


def provider_fee(amount: int, agg_config: dict | None, rate: float) -> int:
    """Frais agrégateur imputés en compta (0 si pris directement au client)."""
    if fee_payer(agg_config) == "customer" or rate <= 0:
        return 0
    return math.ceil(amount * rate)


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
    agg_fee_rate: float = 0.0,
    payout_fee_rate: float = 0.0,
) -> FeeBreakdown:
    """Calcule la ventilation MW pour un payin de `gross` XAF.

    `agg_fee_rate` : taux de frais de DÉPÔT de l'agrégateur.
    `payout_fee_rate` : taux de frais de RETRAIT ; si `prepay_payout_fee`, on les
    retient dès le dépôt ceil(brut x taux) dans la réserve de l'app (par réseau).

    `agg_config` : ligne de la table `aggregators` (défaut agrégateur).
    `app_config` : dict avec mw_commission_type/value issus de la table `apps`
                   (surcharge par app). None ou valeurs NULL = pas de surcharge.
    Montants arrondis à l'entier inférieur (pas de fraction de XAF).
    """
    comm_type, comm_value = _resolve_commission(app_config, agg_config)

    if comm_type == "flat":
        mw_commission = min(int(comm_value), gross)
    else:
        # Arrondi à l'excès : la fraction de franc revient à la plateforme.
        mw_commission = math.ceil(gross * comm_value) if comm_value > 0 else 0

    fee = provider_fee(gross, agg_config, agg_fee_rate)
    if fee_payer(agg_config) == "platform":
        app_amount = gross - mw_commission
        platform_amount = mw_commission - fee
    else:
        mw_commission = min(mw_commission, max(gross - fee, 0))
        app_amount = gross - fee - mw_commission
        platform_amount = mw_commission
    reserve = 0
    if prepays_payout_fee(agg_config) and payout_fee_rate > 0 and app_amount > 0:
        # Réserve de l'APP pour le réseau du dépôt : ceil(brut x taux de payout).
        # Elle couvre toujours les frais exacts d'un retrait vers ce réseau.
        reserve = min(math.ceil(gross * payout_fee_rate), app_amount)
        app_amount -= reserve
    return FeeBreakdown(
        gross=gross,
        provider_fee=fee,
        payout_fee_reserve=reserve,
        mw_commission=mw_commission,
        app_amount=app_amount,
        platform_amount=platform_amount,
    )


def fees_info(
    gross: int,
    agg_config: dict | None,
    app_config: dict | None = None,
    agg_fee_rate: float = 0.0,
    payout_fee_rate: float = 0.0,
) -> dict:
    """Ventilation sous forme de dict (pour les endpoints /fees)."""
    comm_type, comm_value = _resolve_commission(app_config, agg_config)
    b = compute_fees(gross, agg_config, app_config, agg_fee_rate, payout_fee_rate)
    payer = fee_payer(agg_config)
    return {
        "gross": b.gross,
        "fee_payer": payer,
        "aggregator_fee_rate": agg_fee_rate,
        "aggregator_fee": b.provider_fee,
        "payout_fee_reserve": b.payout_fee_reserve,
        "aggregator_fee_note": (
            "Prélevé par l'agrégateur directement sur le client (hors comptabilité MW)."
            if payer == "customer" else
            "Prélevé par l'agrégateur sur le wallet marchand, déduit "
            + ("de la part de l'app." if payer == "app" else "de la commission MobileWallet.")),
        "mw_commission_type": comm_type,
        "mw_commission_value": comm_value,
        "mw_commission": b.mw_commission,
        "app_amount": b.app_amount,
        "platform_amount": b.platform_amount,
    }
