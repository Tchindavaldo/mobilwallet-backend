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
from core.upstream_errors import classify_upstream_error
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
            return resp.json()
    except (httpx.HTTPError, ValueError) as e:
        code = classify_upstream_error(e)
        log.warning("pawapay POST %s: échec (%s)", path, type(e).__name__)
        if code:
            raise PawapayUpstreamError(code) from e
        raise


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
    """GET /v2/{deposits|payouts}/{id}. Retourne {internal, raw, failure, data}
    ou None (erreur réseau, ou NOT_FOUND pas encore visible -> re-essayer)."""
    body = await _get(f"/v2/{kind}/{provider_id}")
    if not body or body.get("status") != "FOUND":
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


def failure_message(failure: dict | None) -> str:
    if not failure:
        return ""
    return failure.get("failureMessage") or failure.get("failureCode") or ""


async def poll_until_final(kind: str, provider_id: str) -> dict:
    """Poll le statut jusqu'à un terminal, en écoutant aussi le webhook.

    Sans timeout (pawaPay finit toujours par trancher, y compris après
    IN_RECONCILIATION). Rythme : 5 s les 5 premières minutes, puis 30 s.
    Retourne {status, raw, failure, settled_by}.
    """
    ev = registry.register(provider_id)
    start = time.monotonic()
    try:
        while True:
            if ev.is_set():
                v = registry.take_verdict(provider_id) or {}
                if v.get("status") in TERMINAL:
                    return {**v, "settled_by": "webhook"}
            res = await fetch_status(kind, provider_id)
            if res is not None:
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
