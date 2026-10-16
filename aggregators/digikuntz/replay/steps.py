"""Etapes HTTP du replay sans navigateur : step1 (transaction) .. step4 (poll verify).
Utilise par aggregator (mode replay) et cli.
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

from .config import (
    _dk,
    DIGIKUNTZ_BASE,
    DIGIKUNTZ_SECRET,
    DIGIKUNTZ_USER_ID,
    HEADERS,
    ReplayConfig,
)
from .interpret import FLW_CODES, interpret_verify

async def step1_create_transaction(
    amount: int, phone: str, email: str, name: str,
    raison: str = "MobileWallet",
) -> dict:
    """Create a digikuntz transaction, return {txRef, paymentLink, amount}.

    `callbackUrl` est transmis à DigiKUNTZ UNIQUEMENT si l'env
    `DIGIKUNTZ_USE_CALLBACK` est activé (on envoie alors `DIGIKUNTZ_CALLBACK_URL`).
    On n'utilise JAMAIS un callback_url reçu dans la requête /pay.
    """
    print(f"[1] Creating digikuntz transaction: {amount} XAF, {phone}")
    body = {
        "estimation": amount,
        "raisonForTransfer": raison,
        "userEmail": email,
        "userPhone": phone,
        "userCountry": "CM",
        "senderName": name,
    }
    if _dk.use_callback and _dk.callback_url:
        body["callbackUrl"] = _dk.callback_url
    log.info("[REPLAY][step1] payload envoyé à DigiKUNTZ: %s", body)
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{DIGIKUNTZ_BASE}/transaction",
            json=body,
            headers={
                "x-user-id": DIGIKUNTZ_USER_ID,
                "x-secret-key": DIGIKUNTZ_SECRET,
                "content-type": "application/json",
            },
        )
        resp.raise_for_status()
        data = resp.json().get("data", resp.json())
        print(f"    txRef={data.get('transactionRef')}")
        print(f"    paymentLink={data.get('paymentLink')}")
        print(f"    paymentWithTaxes={data.get('paymentWithTaxes')}")
        return data


async def step2_initialize_checkout(payment_link: str, cfg: "ReplayConfig | None" = None) -> dict:
    """Load the payment link, initialize checkout, extract RSA public key."""
    cfg = cfg or ReplayConfig.defaults()
    print(f"[2] Initializing checkout...")

    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        # Get the hosted_pay JSON
        link_id = payment_link.split("/")[-1]
        resp2 = await client.get(
            f"https://api.ravepay.co/flwv3-pug/getpaidx/api/hosted_pay/{link_id}?json=1"
        )
        hosted = resp2.json()
        print(f"    hosted_pay status: {hosted.get('status')}")

        # Upgrade checkout
        hosted_data = hosted.get("data", {})
        await client.post(cfg.upgrade_url, json=hosted_data, headers=cfg.headers)

        # Initialize — this returns the RSA public key
        resp4 = await client.post(cfg.init_url, json=hosted_data, headers=cfg.headers)
        init_resp = resp4.json()
        init_data = init_resp.get("data") or {}
        public_key = init_data.get("public_key", "") if init_data else ""
        print(f"    initialize status: {init_resp.get('status')}")
        print(f"    RSA public key: {public_key[:60]}...")

        return {
            "hosted_data": hosted_data,
            "init_data": init_data,
            "public_key": public_key,
        }


async def step3_charge(
    amount: int,
    phone: str,
    network: str,
    email: str,
    firstname: str,
    lastname: str,
    tx_ref: str,
    public_key_rsa: str,
    redirect_url: str = "https://payments.digikuntz.com/payment-done/subscription/packages",
    cfg: "ReplayConfig | None" = None,
) -> dict:
    """Send the charge request using cryptico encryption (same as browser)."""
    cfg = cfg or ReplayConfig.defaults()
    import subprocess

    print(f"[3] Sending charge: {amount} XAF, {network}, {phone}")
    log.info("[REPLAY][step3] charge_url=%s", cfg.charge_url)
    log.info("[REPLAY][step3] PBFPubKey(flw_pub_key)=%r", cfg.pub_key)
    log.info("[REPLAY][step3] public_key_rsa(len=%d)=%r...",
             len(public_key_rsa or ""), (public_key_rsa or "")[:60])

    modalauditid = uuid.uuid4().hex

    # Garde-fou : sans clé RSA, le chiffrement cryptico est impossible. On
    # n'appelle PAS Node avec une clé vide (il renvoie son message d'usage,
    # ensuite déguisé à tort en "panne opérateur"). On signale une erreur
    # technique claire et distincte : le template n'a pas de clé exploitable.
    if not (public_key_rsa or "").strip():
        log.warning("[REPLAY][step3] clé RSA absente -> replay impossible "
                    "(template sans clé et réinitialisation échouée)")
        return {"modalauditid": modalauditid, "charge_response": {
            "status": "error",
            "code": "replay_key_missing",
            "message": ("Clé de chiffrement (RSA) absente : le template de replay "
                        "ne contient pas de clé valide. Relancez un paiement en "
                        "mode 'browser' pour redéduire un template complet."),
        }}
    device_fp = hashlib.sha256(f"{email}{time.time()}".encode()).hexdigest()

    # Build the plaintext payload (exact same structure the browser sends)
    plaintext = json.dumps({
        "amount": amount,
        "campaign_id": None,
        "is_discounted": 0,
        "country": "CM",
        "currency": "XAF",
        "cycle": "one-time",
        "device_fingerprint": device_fp,
        "email": email,
        "firstname": firstname,
        "lastname": lastname,
        "meta": [
            {"metaname": "__CheckoutInitAddress", "metavalue": "N/A"},
            {"metaname": "app", "metavalue": "digikuntz-payments"},
            {"metaname": "env", "metavalue": "development"},
        ],
        "modalauditid": modalauditid,
        "payment_type": "mobilemoneyfranco",
        "PBFPubKey": cfg.pub_key,
        "redirect_url": redirect_url,
        "txRef": tx_ref,
        "is_mobile_money_franco": True,
        "network": network,
        "phonenumber": phone,
    }, separators=(",", ":"))

    # Encrypt with REAL cryptico.js via Node.js — identical to browser.
    # encrypt.js lives in core/crypto/ (repo root is two levels up from this module).
    repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    encrypt_js = os.path.join(repo_root, "core", "crypto", "encrypt.js")
    proc = subprocess.run(
        ["node", encrypt_js, plaintext, public_key_rsa],
        capture_output=True, text=True, timeout=15,
    )
    if proc.returncode != 0:
        print(f"    ERROR: cryptico encryption failed: {proc.stderr}")
        return {"modalauditid": modalauditid, "charge_response": {"status": "error", "message": proc.stderr}}
    encrypted = proc.stdout.strip()
    print(f"    encrypted payload: {len(encrypted)} chars")

    # Send charge request (same format as captured from browser)
    charge_body = {
        "modalauditid": modalauditid,
        "PBFPubKey": cfg.pub_key,
        "client": encrypted,
    }

    # The /charge call can transiently fail (empty/non-JSON body on 502/504,
    # ReadTimeout, connection reset). Retry up to 3 times before giving up.
    max_attempts = 3
    last_error = ""
    async with httpx.AsyncClient(timeout=60) as client:
        for attempt in range(1, max_attempts + 1):
            try:
                resp = await client.post(
                    cfg.charge_url,
                    json=charge_body,
                    headers=cfg.headers,
                )
                result = resp.json()
            except (httpx.HTTPError, json.JSONDecodeError) as e:
                last_error = f"{type(e).__name__}"
                print(f"    charge tentative {attempt}/{max_attempts} échouée "
                      f"({last_error}), réponse invalide/réseau instable...")
                continue
            print(f"    charge status: {resp.status_code} (tentative {attempt})")
            print(f"    response: {json.dumps(result, indent=2)[:500]}")
            log.info("[REPLAY][step3] HTTP %s tentative %d -> réponse brute: %s",
                     resp.status_code, attempt, json.dumps(result)[:1500])

            # "demande longue" — poll ping_url until we get the real charge result.
            ping_url = (result.get("data") or {}).get("ping_url", "")
            if ping_url and result.get("status") == "success":
                print(f"\n[3b] Polling ping_url for charge result...")
                async with httpx.AsyncClient(timeout=30) as ping_client:
                    for i in range(20):
                        await asyncio.sleep(3)
                        try:
                            pr = await ping_client.get(ping_url, headers=cfg.headers)
                            ping_data = pr.json()
                        except Exception:
                            continue
                        print(f"    ping {i+1}: {json.dumps(ping_data)[:400]}")
                        inner = ping_data.get("data", {})
                        if isinstance(inner, dict):
                            resp_obj = inner.get("response_parsed")
                            if not isinstance(resp_obj, dict):
                                resp_str = inner.get("response", "")
                                if isinstance(resp_str, str) and resp_str.startswith("{"):
                                    try:
                                        resp_obj = json.loads(resp_str)
                                    except json.JSONDecodeError:
                                        resp_obj = {}
                            if isinstance(resp_obj, dict):
                                nested = resp_obj.get("data", {})
                                if isinstance(nested, dict) and (nested.get("flw_reference") or nested.get("flwRef")):
                                    print(f"    Got real charge response from ping_url")
                                    result = resp_obj
                                    break
                                # Échec Flutterwave IMBRIQUÉ dans data.response :
                                # le ping a un status 'success/completed' mais le
                                # vrai résultat de charge est un code d'erreur
                                # (FLW_ERR, etc.). On NE doit PAS continuer à
                                # poller : on remonte cette erreur comme réponse de
                                # charge pour qu'interpret_charge la traite.
                                err_code = resp_obj.get("code", "")
                                if err_code in FLW_CODES or err_code == "FLW_ERR":
                                    print(f"    ping_url -> échec charge (code={err_code})")
                                    log.warning("[REPLAY][step3b] ping_url échec charge: %s",
                                                json.dumps(resp_obj)[:500])
                                    # Forme reconnue par interpret_charge (status=error + data).
                                    result = {"status": "error",
                                              "message": resp_obj.get("message", ""),
                                              "data": {"code": err_code}}
                                    break
                        if ping_data.get("status") == "error":
                            result = ping_data
                            break

            return {"modalauditid": modalauditid, "charge_response": result}

    # Les 3 tentatives ont échoué.
    print(f"    ABANDON: charge échouée après {max_attempts} tentatives ({last_error})")
    return {"modalauditid": modalauditid, "charge_response": {
        "status": "error",
        "message": f"Réponse Flutterwave invalide après {max_attempts} tentatives "
                   f"({last_error}). Réessayez.",
    }}


async def step4_poll_verify(
    modalauditid: str, flw_ref: str, cfg: "ReplayConfig | None" = None
) -> dict:
    """Poll the verify endpoint until payment is confirmed or failed.

    PAS DE TIMEOUT : on poll jusqu'à un verdict terminal. L'opérateur tranche
    toujours (Orange/MTN basculent une transaction non validée en échec après
    leur propre délai), donc un terminal finit toujours par arriver.
    A single network hiccup on one poll (ReadTimeout, reset, non-JSON body) is
    ignored: we skip that tick and keep polling until a final verdict arrives.
    """
    cfg = cfg or ReplayConfig.defaults()
    print(f"[4] Polling verify (flw_ref={flw_ref})... (sans timeout)")

    start = time.monotonic()
    i = 0
    last_status = None

    async with httpx.AsyncClient(timeout=30) as client:
        while True:
            await asyncio.sleep(1)
            i += 1
            try:
                resp = await client.post(
                    cfg.verify_url,
                    json={
                        "modalauditid": modalauditid,
                        "PBFPubKey": cfg.pub_key,
                        "flw_ref": flw_ref,
                    },
                    headers=cfg.headers,
                )
                data = resp.json()
            except (httpx.HTTPError, json.JSONDecodeError) as e:
                print(f"    poll {i}: réseau instable ({type(e).__name__}), on continue...")
                continue

            status = data.get("data", {}).get("status", "pending")
            charge_code = data.get("data", {}).get("chargeResponseCode", "")
            elapsed = int(time.monotonic() - start)
            # Log every 10th poll, plus ALWAYS on a status change (the moment
            # the operator decides) so we can pinpoint the exact delay.
            if status != last_status or i % 10 == 0:
                mark = "  <-- CHANGEMENT" if status != last_status and last_status is not None else ""
                print(f"    poll {i} (t+{elapsed}s = {elapsed//60}min{elapsed%60:02d}): "
                      f"status={status} code={charge_code}{mark}")
                last_status = status

            # Parse the structured verify response directly.
            verdict = interpret_verify(data, "")
            if verdict and verdict[0] in ("successful", "failed", "cancelled"):
                return data
            if status in ("successful", "failed"):
                return data
            if charge_code == "00":
                return data


