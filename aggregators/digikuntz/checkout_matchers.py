"""Reconnaissance des requêtes du checkout Flutterwave embarqué par DigiKUNTZ.

Propre à DigiKUNTZ : `core/` reste neutre et ne connaît aucun prestataire.
"""

from core.browser import CapturedRequest


def is_charge_request(r: CapturedRequest) -> bool:
    """POST /charge du checkout Flutterwave (déclenche l'USSD)."""
    return (
        r.method == "POST"
        and ("flutterwave" in r.url or "ravepay" in r.url)
        and "/charge" in r.url
    )


def is_verify_request(r: CapturedRequest) -> bool:
    """POST /verify/ de polling du checkout Flutterwave."""
    return r.method == "POST" and "ravepay" in r.url and "/verify/" in r.url
