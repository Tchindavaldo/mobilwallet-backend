"""Interpretation des reponses Flutterwave (charge / verify / ping) en (statut, message).
Inclut les libelles reseau et messages USSD. Utilise par steps, cli, aggregator, status_poll.
"""

import asyncio
import base64
import hashlib
import json
import os
import sys
import time
import uuid

import httpx
import logging

log = logging.getLogger("ai_browser2")

# ---------------------------------------------------------------------------
# Flutterwave response → message clair
# Basé sur les vraies réponses capturées dans nos tests.
# ---------------------------------------------------------------------------

# chargeResponseCode → (statut, message)
CHARGE_CODES = {
    "00": ("successful", "Paiement validé avec succès."),
    "02": ("pending", "En attente de validation USSD par l'utilisateur."),
    "R1": ("failed", None),  # None = dépend du contexte (réseau / refus)
    "RR": ("failed", None),
    "XX": ("failed", None),
}

# data.code / status → (statut, message)
FLW_CODES = {
    "FLW_ERR": ("failed", None),
}

# Flutterwave data.status → (statut, message)
FLW_STATUSES = {
    "successful": ("successful", "Paiement validé avec succès."),
    "success-completed": ("successful", "Paiement validé avec succès."),
    "failed": ("failed", None),
    "cancelled": ("cancelled", "Paiement annulé / non validé sur le USSD "
                  "(par l'utilisateur ou l'opérateur)."),
    "success-pending-validation": ("pending", "En attente de validation USSD."),
}


def alt_network(network: str) -> str:
    n = (network or "").lower()
    if "orange" in n:
        return "MTN"
    if "mtn" in n:
        return "Orange"
    return "un autre réseau"


def network_label(network: str) -> str:
    n = (network or "").lower()
    if "orange" in n:
        return "Orange Money"
    if "mtn" in n:
        return "MTN Mobile Money"
    return network or "ce réseau"


def ussd_code(network: str) -> str:
    """Code USSD à composer pour valider, selon le réseau (Cameroun)."""
    n = (network or "").lower()
    if "orange" in n:
        return "#150*50#"
    if "mtn" in n:
        return "*126#"
    return ""


def ussd_message(network: str) -> str:
    """Message client clair invitant à composer le code USSD du réseau."""
    code = ussd_code(network)
    if code:
        return (f"Composez {code} sur votre téléphone pour valider la transaction "
                f"{network_label(network)}.")
    return "Composez le code USSD reçu sur votre téléphone pour valider la transaction."


def interpret_charge(resp: dict, network: str) -> tuple[str, str] | None:
    """Interpret a /charge or ping_url response.

    Returns (status, message_clair) or None if no definitive verdict.
    At the charge stage, USSD hasn't been sent yet, so any failure = network.
    """
    # Top-level error
    if resp.get("status") == "error":
        data = resp.get("data", {})
        log.warning("[REPLAY][interpret_charge] status=error -> réponse Flutterwave brute: %s",
                    json.dumps(resp)[:1500])
        # Erreur TECHNIQUE LOCALE (pas une réponse Flutterwave) : chiffrement
        # impossible, clé absente, etc. On NE la déguise PAS en panne opérateur.
        # Elle se reconnaît à l'absence de structure Flutterwave (pas de data
        # dict porteur de code/err_tx) et/ou à un code local connu.
        local_code = resp.get("code", "")
        if local_code == "replay_key_missing" or not isinstance(data, dict) or not data:
            return "error", resp.get("message", "Erreur technique lors du replay.")
        if isinstance(data, dict):
            code = data.get("code", "")
            err_tx = data.get("err_tx", {})
            rc = err_tx.get("chargeResponseCode", "") if isinstance(err_tx, dict) else ""
            flw_msg = resp.get("message", "")
            log.warning("[REPLAY][interpret_charge] code=%r chargeResponseCode=%r message=%r",
                        code, rc, flw_msg)
            low = f"{code} {rc} {flw_msg}".lower()
            # Solde insuffisant (code 51) = problème de compte, PAS réseau.
            if rc == "51" or "insufficient" in low or "solde insuffisant" in low:
                return "failed", (
                    f"Solde insuffisant sur le compte {network_label(network)}. "
                    f"Veuillez recharger puis réessayer."
                )
            # Sinon, échec au stade /charge (avant tout USSD) = réseau opérateur
            # dérangé. Statut 'network_down' (aligné avec le moteur navigateur)
            # pour que /pay renvoie un 503 operator_unavailable cohérent.
            msg = (f"Le réseau {network_label(network)} est actuellement dérangé. "
                   f"Veuillez réessayer avec {alt_network(network)}.")
            detail = f" [code={code}, chargeResponseCode={rc}, message={flw_msg}]"
            return "network_down", msg + detail
    return None


def interpret_verify(resp: dict, network: str) -> tuple[str, str] | None:
    """Interpret a /verify/mpesa response.

    L'USSD A ÉTÉ ENVOYÉ à ce stade. Or un solde insuffisant NE déclenche PAS
    d'USSD (il échoue au /charge, avant). Donc tout ÉCHEC après USSD = non-validation
    sur le téléphone (refus de l'utilisateur OU expiration côté opérateur) ->
    'cancelled' (et non 'failed'). 'cancelled' bloque le numéro pendant le délai
    anti-doublon. Vaut pour les DEUX moteurs (replay + navigateur partagent cette fn).
    """
    data = resp.get("data", {})
    if not isinstance(data, dict):
        return None

    status = data.get("status", "")
    rc = data.get("chargeResponseCode", "")

    # Check chargeResponseCode first (most precise)
    if rc in CHARGE_CODES:
        st, msg = CHARGE_CODES[rc]
        if st == "successful":
            return "successful", msg
        if st == "failed":
            return "cancelled", ("Paiement annulé / non validé sur le USSD "
                                 "(par l'utilisateur ou l'opérateur).")
        # pending (code 02) → the USSD was sent. The final verdict can still
        # arrive via data.status (e.g. status=failed while code stays 02), so
        # don't stop at the code — fall through to the status check below.

    # Check data.status
    if status in FLW_STATUSES:
        st, msg = FLW_STATUSES[status]
        if st in ("successful", "cancelled"):
            return st, msg
        if st == "failed":
            # Échec APRÈS USSD = refus utilisateur -> cancelled (cf. docstring).
            return "cancelled", ("Paiement annulé / non validé sur le USSD "
                                 "(par l'utilisateur ou l'opérateur).")
        # pending → continue
        return None

    return None


def interpret_ping(ping_data: dict, network: str) -> tuple[str, str] | None:
    """Interpret a ping_url response (used for 'demande longue').

    The real response is often nested as a JSON string in data.response.
    """
    inner = ping_data.get("data", {})
    if not isinstance(inner, dict):
        return None

    # data.response can be a JSON string with the real charge result
    resp_str = inner.get("response", "")
    if isinstance(resp_str, str) and resp_str.startswith("{"):
        try:
            resp_obj = json.loads(resp_str)
            code = resp_obj.get("code", "")
            if code in FLW_CODES:
                msg = (f"Le réseau {network_label(network)} est actuellement dérangé. "
                       f"Veuillez réessayer avec {alt_network(network)}.")
                return "failed", msg + f" [code={code}]"
            # Could also contain success data
            flw_ref = resp_obj.get("flw_reference", "") or resp_obj.get("flwRef", "")
            if flw_ref:
                return None  # Not a failure, has a flw_ref → continue flow
        except json.JSONDecodeError:
            pass

    return None

# digiKUNTZ + Flutterwave config — loaded from central settings (.env).
# These are DEFAULTS for replay; a DB template overrides the Flutterwave
