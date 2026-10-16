"""Point d entree CLI du replay (test manuel) : python -m aggregators.digikuntz.replay.cli.
Enchaine step1..step4 et affiche le verdict.
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

from .config import HEADERS
from .interpret import interpret_charge, interpret_ping, interpret_verify
from .steps import step1_create_transaction, step2_initialize_checkout, step3_charge, step4_poll_verify

async def main():
    amount = int(sys.argv[1]) if len(sys.argv) > 1 else 25
    phone = sys.argv[2] if len(sys.argv) > 2 else "696080087"
    network = sys.argv[3] if len(sys.argv) > 3 else "Orangemoney"
    email = sys.argv[4] if len(sys.argv) > 4 else "tchindatchenetsuvaldoblair@gmail.com"
    name = sys.argv[5] if len(sys.argv) > 5 else "Blair"

    print(f"\n=== Flutterwave Direct Charge (No Browser) ===")
    print(f"Amount: {amount} XAF | Phone: {phone} | Network: {network}\n")

    # Step 1: Create digikuntz transaction
    tx = await step1_create_transaction(amount, phone, email, name)
    tx_ref = tx.get("transactionRef", "")
    payment_link = tx.get("paymentLink", "")
    total = int(tx.get("paymentWithTaxes", amount))

    if not payment_link:
        print("ERROR: No payment link returned")
        return

    # Step 2: Initialize checkout and get RSA public key
    checkout = await step2_initialize_checkout(payment_link)
    public_key_rsa = checkout.get("public_key", "")

    if not public_key_rsa:
        # Fallback to the key we captured from browser tests
        public_key_rsa = "baA/RgjURU3I0uqH3iRos3NbE8fT+lP8SDXKymsnfdPrMQAEoMBuXtoaQiJ1i5tuBG9EgSEOH1LAZEaAsvwClw=="
        print(f"    Using fallback RSA key")

    # Step 3: Charge with cryptico encryption
    charge = await step3_charge(
        amount=total,
        phone=phone,
        network=network,
        email=email,
        firstname="API",
        lastname=f"Call: {name}",
        tx_ref=tx_ref,
        public_key_rsa=public_key_rsa,
    )

    charge_resp = charge.get("charge_response", {})
    charge_data = charge_resp.get("data", {})

    # Interpret directly from JSON structure (no keyword matching needed).
    verdict = interpret_charge(charge_resp, network)
    if verdict:
        status, msg = verdict
        print(f"\n=== RESULTAT: {status.upper()} ===")
        print(f"    {msg}")
        return

    flw_ref = ""
    if charge_data:
        flw_ref = charge_data.get("flw_ref", "")
        if not flw_ref:
            nested = charge_data.get("data", {})
            if nested:
                flw_ref = nested.get("flw_reference", "")

        # Handle "demande longue" — poll ping_url for the real response
        ping_url = charge_data.get("ping_url", "")
        if ping_url and not flw_ref:
            print(f"\n[3b] Polling ping_url for charge result...")
            async with httpx.AsyncClient(timeout=30) as client:
                for i in range(20):
                    await asyncio.sleep(3)
                    resp = await client.get(ping_url, headers=HEADERS)
                    ping_data = resp.json()
                    print(f"    ping {i+1}: {json.dumps(ping_data)[:200]}")

                    # Parse the structured ping response directly.
                    verdict = interpret_ping(ping_data, network)
                    if verdict:
                        status, msg = verdict
                        print(f"\n=== RESULTAT: {status.upper()} ===")
                        print(f"    {msg}")
                        return

                    inner = ping_data.get("data", {})
                    if isinstance(inner, dict):
                        # The real charge result lives in data.response — a JSON
                        # *string* (or already-parsed in data.response_parsed).
                        resp_obj = inner.get("response_parsed")
                        if not isinstance(resp_obj, dict):
                            resp_str = inner.get("response", "")
                            if isinstance(resp_str, str) and resp_str.startswith("{"):
                                try:
                                    resp_obj = json.loads(resp_str)
                                except json.JSONDecodeError:
                                    resp_obj = {}
                        if isinstance(resp_obj, dict):
                            nested2 = resp_obj.get("data", {})
                            if isinstance(nested2, dict):
                                flw_ref = nested2.get("flw_reference", "") or nested2.get("flwRef", "")
                                if flw_ref:
                                    print(f"    Got flw_ref: {flw_ref}")
                                    print(f"    note: {nested2.get('note', '')}")
                                    break
                    status = ping_data.get("status", "")
                    if status == "error":
                        print(f"    Charge failed: {ping_data.get('message', '')}")
                        break

    if not flw_ref:
        print(f"\nERROR: No flw_ref in charge response")
        print(f"Full response: {json.dumps(charge_resp, indent=2)[:1000]}")
        return

    print(f"\n    flw_ref: {flw_ref}")
    print(f"    Compose #150*50# sur ton telephone pour valider!\n")

    # Step 4: Poll verify. The USSD WAS sent, so a failure = user refused / no funds.
    verify = await step4_poll_verify(charge["modalauditid"], flw_ref)
    verdict = interpret_verify(verify, network)
    if verdict:
        status, msg = verdict
    else:
        status = verify.get("data", {}).get("status", "unknown")
        msg = f"Statut Flutterwave: {status}"
    print(f"\n=== RESULTAT: {status.upper()} ===")
    print(f"    {msg}")
    print(json.dumps(verify, indent=2)[:400])


if __name__ == "__main__":
    asyncio.run(main())
