"""Client HTTP pawaPay Merchant API v2 (dépôts, payouts, statut, soldes).

API pure (pas de navigateur, pas de LLM) : auth `Authorization: Bearer <token>`,
base sandbox `https://api.sandbox.pawapay.io` ou prod `https://api.pawapay.io`.
Doc : https://docs.pawapay.io/v2/docs/welcome

Statuts pawaPay -> internes (FAITS renvoyés par l'API, pas des verdicts déduits) :
  ACCEPTED / PROCESSING / ENQUEUED / IN_RECONCILIATION -> pending
  COMPLETED -> successful | FAILED -> failed
  FAILED + failureCode PAYMENT_NOT_APPROVED / MANUALLY_CANCELLED -> cancelled
À l'initiation : REJECTED -> failed (rien n'est parti), DUPLICATE_IGNORED -> pending.
"""

import asyncio
import logging
import time
import uuid

import httpx

from core.config import settings
from core.upstream_errors import NETWORK_UNAVAILABLE, classify_upstream_error
from core.verdict_registry import VerdictRegistry

log = logging.getLogger("ai_browser2")

_pp = settings.pawapay

# Réseau canonique MobileWallet -> code provider pawaPay (Cameroun).
PROVIDERS = {
    "MTN": "MTN_MOMO_CMR",
    "Orangemoney": "ORANGE_CMR",
    "ORANGEMONEY": "ORANGE_CMR",
}

STATUS_MAP = {
    "ACCEPTED": "pending",
    "PROCESSING": "pending",
    "ENQUEUED": "pending",
    "IN_RECONCILIATION": "pending",
    "DUPLICATE_IGNORED": "pending",
    "COMPLETED": "successful",
    "FAILED": "failed",
    "REJECTED": "failed",
}

# Codes d'échec pawaPay qui signifient « non autorisé / annulé » plutôt qu'échec.
_CANCEL_CODES = {"PAYMENT_NOT_APPROVED", "MANUALLY_CANCELLED"}

TERMINAL = {"successful", "failed", "cancelled"}

# failureCode pawaPay -> message FR affiché au client (dépôts, payouts, rejets).
FAILURE_MESSAGES_FR = {
    "INSUFFICIENT_BALANCE": "Solde insuffisant sur le compte Mobile Money.",
    "PAYMENT_NOT_APPROVED": "Paiement non validé : la demande de code PIN a été refusée ou a expiré.",
    "MANUALLY_CANCELLED": "Vous avez annulé manuellement le paiement.",
    "PAYER_NOT_FOUND": "Ce numéro n'a pas de compte Mobile Money actif.",
    "RECIPIENT_NOT_FOUND": "Le numéro bénéficiaire n'a pas de compte Mobile Money actif.",
    "PAYER_LIMIT_REACHED": "Plafond de transactions atteint sur ce compte Mobile Money.",
    "RECIPIENT_LIMIT_REACHED": "Plafond de réception atteint sur le compte bénéficiaire.",
    "PAYMENT_IN_PROGRESS": "Un autre paiement est déjà en cours sur ce numéro. Réessayez dans quelques minutes.",
    "WALLET_LIMIT_REACHED": "Plafond du compte Mobile Money atteint.",
    "UNSPECIFIED_FAILURE": "L'opérateur a refusé le paiement sans préciser la raison.",
    "UNKNOWN_ERROR": "Erreur inconnue chez l'opérateur.",
    "INVALID_PHONE_NUMBER": "Numéro de téléphone invalide.",
    "INVALID_AMOUNT": "Montant invalide.",
    "AMOUNT_OUT_OF_BOUNDS": "Montant hors des limites autorisées par l'opérateur.",
    "INVALID_PROVIDER": "Opérateur non pris en charge pour ce numéro.",
    "PROVIDER_TEMPORARILY_UNAVAILABLE": "{net} est momentanément indisponible. Réessayez plus tard ou utilisez {other}.",
    "INVALID_CURRENCY": "Devise non prise en charge.",
    "DUPLICATE_METADATA_FIELD": "Requête invalide.",
    "INVALID_INPUT": "Requête invalide.",
    "AUTHENTICATION_ERROR": "Service de paiement mal configuré (authentification).",
    "AUTHORISATION_ERROR": "Opération non autorisée sur le compte marchand.",
    "INSUFFICIENT_BALANCE_MERCHANT": "Solde marchand insuffisant pour effectuer le virement.",
}

# Un dépôt/payout introuvable chez pawaPay au-delà de ce délai n'a jamais été
# créé (fait mécanique, pas un verdict de paiement) -> on arrête de poller.
NOT_FOUND_GIVE_UP_S = 600

# Coordination webhook /webhook/pawapay ⇄ polling (indexé par depositId/payoutId).
registry = VerdictRegistry()


class PawapayUpstreamError(Exception):
    """API pawaPay injoignable (5xx/réseau) : rien n'est parti chez l'opérateur."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def new_id() -> str:
    """UUIDv4 exigé par pawaPay pour depositId / payoutId."""
    return str(uuid.uuid4())


def msisdn(phone: str) -> str:
    """Numéro camerounais au format MSISDN (237XXXXXXXXX, chiffres seuls)."""
    digits = "".join(c for c in (phone or "") if c.isdigit())
    if digits.startswith("00237"):
        digits = digits[2:]
    if not digits.startswith("237"):
        digits = "237" + digits
    return digits


def customer_message(label: str) -> str:
    """customerMessage pawaPay : 4-22 caractères alphanumériques/espaces."""
    clean = "".join(c for c in (label or "") if c.isalnum() or c == " ").strip()
    clean = clean[:22].strip()
    return clean if len(clean) >= 4 else "MobileWallet"


def map_status(raw: str, failure: dict | None = None) -> str:
    """Statut pawaPay brut (+ failureReason) -> statut interne."""
    internal = STATUS_MAP.get((raw or "").upper(), "unknown")
    if internal == "failed" and (failure or {}).get("failureCode") in _CANCEL_CODES:
        return "cancelled"
    return internal


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {_pp.api_token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


async def _post(path: str, body: dict) -> dict:
    """POST {base}{path}. Lève PawapayUpstreamError sur panne amont (5xx/réseau).

    Les refus métier pawaPay (400/403 avec status REJECTED) sont renvoyés tels
    quels pour que l'appelant lise `failureReason`."""
    url = f"{_pp.base.rstrip('/')}{path}"
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(url, json=body, headers=_headers())
            if resp.status_code >= 500:
                resp.raise_for_status()
            data = resp.json()
    except (httpx.HTTPError, ValueError) as e:
        code = classify_upstream_error(e)
        log.warning("pawapay POST %s: échec (%s)", path, type(e).__name__)
        if code:
            raise PawapayUpstreamError(code) from e
        raise
    if resp.status_code in (401, 403) and not data.get("status"):
        # Token invalide/expiré : rien n'est parti chez l'opérateur.
        log.error("pawapay POST %s: authentification refusée (HTTP %s) %s",
                  path, resp.status_code, str(data)[:300])
        raise PawapayUpstreamError(NETWORK_UNAVAILABLE)
    return data


async def _get(path: str, params: dict | None = None) -> dict | None:
    """GET {base}{path}. None sur erreur réseau/HTTP (l'appelant re-essaie)."""
    url = f"{_pp.base.rstrip('/')}{path}"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, params=params, headers=_headers())
            resp.raise_for_status()
            return resp.json()
    except (httpx.HTTPError, ValueError) as e:
        log.warning("pawapay GET %s: échec (%s)", path, type(e).__name__)
        return None


async def initiate_deposit(deposit_id: str, amount: int, phone: str, network: str,
                           label: str, client_ref: str = "") -> dict:
    """POST /v2/deposits (payin). Retourne {raw, internal, failure, body, response}."""
    body = {
        "depositId": deposit_id,
        "amount": str(int(amount)),
        "currency": _pp.currency,
        "payer": {"type": "MMO", "accountDetails": {
            "phoneNumber": msisdn(phone), "provider": PROVIDERS[network]}},
        "customerMessage": customer_message(label),
    }
    if client_ref:
        body["clientReferenceId"] = client_ref
    resp = await _post("/v2/deposits", body)
    raw = resp.get("status", "")
    failure = resp.get("failureReason")
    return {"raw": raw, "internal": map_status(raw, failure), "failure": failure,
            "body": body, "response": resp}


async def initiate_payout(payout_id: str, amount: int, phone: str, network: str,
                          label: str, client_ref: str = "") -> dict:
    """POST /v2/payouts. Retourne {raw, internal, failure, body, response}."""
    body = {
        "payoutId": payout_id,
        "amount": str(int(amount)),
        "currency": _pp.currency,
        "recipient": {"type": "MMO", "accountDetails": {
            "phoneNumber": msisdn(phone), "provider": PROVIDERS[network]}},
        "customerMessage": customer_message(label),
    }
    if client_ref:
        body["clientReferenceId"] = client_ref
    resp = await _post("/v2/payouts", body)
    raw = resp.get("status", "")
    failure = resp.get("failureReason")
    return {"raw": raw, "internal": map_status(raw, failure), "failure": failure,
            "body": body, "response": resp}


async def fetch_status(kind: str, provider_id: str) -> dict | None:
    """GET /v2/{deposits|payouts}/{id}. Retourne {internal, raw, failure, data},
    {internal: "not_found"} si pawaPay ne connaît pas l'id, ou None (erreur
    réseau -> re-essayer)."""
    body = await _get(f"/v2/{kind}/{provider_id}")
    if not body:
        return None
    if body.get("status") == "NOT_FOUND":
        return {"internal": "not_found", "raw": "NOT_FOUND", "failure": None, "data": {}}
    if body.get("status") != "FOUND":
        return None
    data = body.get("data") or {}
    raw = data.get("status", "")
    failure = data.get("failureReason")
    return {"internal": map_status(raw, failure), "raw": raw,
            "failure": failure, "data": data}


async def fetch_balances() -> list | None:
    """GET /v2/wallet-balances?country=... -> [{country, balance, currency, provider}]."""
    body = await _get("/v2/wallet-balances", {"country": _pp.country})
    return None if body is None else body.get("balances", [])


async def fetch_active_conf() -> dict | None:
    """GET /v2/active-conf?country=... : configuration du compte marchand
    (opérateurs actifs, montants min/max par opération). Réponse brute pawaPay."""
    return await _get("/v2/active-conf", {"country": _pp.country})


_NETWORK_NAMES = {"orange": "Orange Money", "mtn": "MTN Mobile Money"}


def _network_names(network: str | None) -> tuple[str, str]:
    """(réseau concerné, autre réseau) en clair ; génériques si réseau inconnu."""
    key = "orange" if (network or "").lower().startswith("orange") else (
        "mtn" if (network or "").lower().startswith("mtn") else "")
    if not key:
        return "L'opérateur Mobile Money", "un autre réseau"
    other = "mtn" if key == "orange" else "orange"
    return _NETWORK_NAMES[key], _NETWORK_NAMES[other]


def failure_message(failure: dict | None, network: str | None = None) -> str:
    """Message FR pour le client ; repli sur le message pawaPay si code inconnu.

    `network` (Orangemoney/MTN/ORANGEMONEY) permet de nommer le réseau concerné."""
    if not failure:
        return ""
    code = failure.get("failureCode") or ""
    msg = FAILURE_MESSAGES_FR.get(code)
    if msg:
        net, other = _network_names(network)
        return msg.format(net=net, other=other)
    return failure.get("failureMessage") or code


async def poll_until_final(kind: str, provider_id: str) -> dict:
    """Poll le statut jusqu'à un terminal, en écoutant aussi le webhook.

    Sans timeout (pawaPay finit toujours par trancher, y compris après
    IN_RECONCILIATION). Rythme : 5 s les 5 premières minutes, puis 30 s.
    Retourne {status, raw, failure, settled_by}.
    """
    ev = registry.register(provider_id)
    start = time.monotonic()
    not_found_since: float | None = None
    try:
        while True:
            if ev.is_set():
                v = registry.take_verdict(provider_id) or {}
                if v.get("status") in TERMINAL:
                    return {**v, "settled_by": "webhook"}
            res = await fetch_status(kind, provider_id)
            if res is not None and res["internal"] == "not_found":
                now = time.monotonic()
                not_found_since = not_found_since or now
                if now - not_found_since >= NOT_FOUND_GIVE_UP_S:
                    log.error("pawapay poll %s %s: introuvable depuis %ss -> abandon",
                              kind, provider_id, NOT_FOUND_GIVE_UP_S)
                    return {"status": "error", "raw": "NOT_FOUND",
                            "failure": None, "settled_by": "polling"}
            elif res is not None:
                not_found_since = None
                log.info("pawapay poll %s %s: %s -> %s", kind, provider_id,
                         res["raw"], res["internal"])
                if res["internal"] in TERMINAL:
                    registry.deliver(provider_id, {"status": res["internal"],
                                                   "raw": res["raw"],
                                                   "failure": res["failure"]})
                    return {"status": res["internal"], "raw": res["raw"],
                            "failure": res["failure"], "settled_by": "polling"}
            tick = 5 if time.monotonic() - start < 300 else 30
            try:
                await asyncio.wait_for(ev.wait(), timeout=tick)
            except asyncio.TimeoutError:
                pass
    finally:
        registry.close(provider_id)


async def wait_webhook(provider_id: str) -> dict:
    """Attend le verdict terminal livré par le webhook, SANS interroger pawaPay
    (pas de polling). Retourne {status, raw, failure, settled_by}."""
    ev = registry.register(provider_id)
    try:
        while True:
            v = registry.take_verdict(provider_id) or {}
            if v.get("status") in TERMINAL:
                return {**v, "settled_by": v.get("settled_by", "webhook")}
            ev.clear()
            await ev.wait()
    finally:
        registry.close(provider_id)
