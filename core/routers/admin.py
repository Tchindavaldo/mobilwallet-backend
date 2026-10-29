"""Endpoints admin : gestion des developers / apps / clés API + execution paiements.

Tous protégés par `require_admin` (header X-Admin-Key). C'est ici qu'on crée les
comptes intégrateurs, qu'on génère/révoque leurs clés, et qu'on peut lancer des
paiements pour une app. La clé en clair n'est renvoyée qu'à sa création (jamais
ré-exposée ensuite).
"""

from fastapi import APIRouter, Depends, HTTPException, Query

from core import auth, registry, tenants, treasury
from core.auth import require_admin
from core.db import db
from core.fees import FEE_PAYERS, fee_network
from core.schemas.admin import (
    AdminPayoutRequest, AdminPayRequest, AggregatorConfigUpdate, AppCommissionUpdate,
    ApiKeyCreate, ApiKeyCreated, AppCreate, DeveloperCreate, PlatformCredit,
)
from core.schemas.payments import PayResponse
from core.schemas.payout import PayoutRequest, PayoutResponse
from core.routers.payments import _execute_payment
from core.routers.payout import _execute_payout, execute_platform_payout

router = APIRouter(
    prefix="/admin",
    tags=["admin"],
    dependencies=[Depends(require_admin)],
    responses={401: {"description": "X-Admin-Key invalide ou manquant."},
               503: {"description": "Supabase non configuré."}},
)


def _require_db(row):
    """503 si la persistance est désactivée (les CRUD renvoient None)."""
    if row is None:
        raise HTTPException(503, "Supabase non configuré — gestion multi-tenant indisponible.")
    return row


@router.post("/developers", summary="Créer un developer (compte intégrateur)")
async def create_developer(body: DeveloperCreate):
    return _require_db(await tenants.create_developer(body.email, body.name))


@router.post("/apps", summary="Créer une app pour un developer")
async def create_app(body: AppCreate):
    """Génère le `webhook_secret` HMAC de l'app (utilisé pour signer les webhooks).
    Le secret n'est pas renvoyé ; il sert côté serveur à la signature."""
    secret = auth.generate_secret()
    app = _require_db(await tenants.create_app(
        body.developer_id, body.name, None, secret))
    return app


@router.get("/developers/{developer_id}/apps", summary="Lister les apps d'un developer")
async def list_apps(developer_id: int):
    return {"apps": await tenants.list_apps(developer_id)}


@router.post("/apps/{app_id}/keys", response_model=ApiKeyCreated,
             summary="Générer une clé API pour une app (clé en clair montrée 1 fois)")
async def create_key(app_id: int, body: ApiKeyCreate):
    if body.env not in ("test", "live"):
        raise HTTPException(400, "env doit être 'test' ou 'live'.")
    full_key, key_prefix, key_hash, last_four = auth.generate_api_key(body.env)
    row = _require_db(await tenants.insert_api_key(
        app_id, body.env, key_prefix, key_hash, last_four))
    return ApiKeyCreated(
        id=row["id"], app_id=app_id, env=body.env,
        api_key=full_key, key_prefix=key_prefix, last_four=last_four,
    )


@router.get("/apps/{app_id}/keys", summary="Lister les clés d'une app (sans la clé en clair)")
async def list_keys(app_id: int):
    return {"keys": await tenants.list_api_keys(app_id)}


@router.post("/apps/{app_id}/keys/{key_id}/revoke", summary="Révoquer une clé API")
async def revoke_key(app_id: int, key_id: int):
    """Désactive la clé (effet immédiat : purge aussi le cache de résolution)."""
    row = await tenants.revoke_api_key(key_id)
    if row is None:
        raise HTTPException(404, f"Aucune clé active avec l'id {key_id}.")
    if row.get("key_hash"):
        auth.invalidate_cache(row["key_hash"])
    return {"revoked": {"id": row["id"], "app_id": row.get("app_id"),
                        "env": row.get("env"), "is_active": row.get("is_active")}}


@router.post(
    "/apps/{app_id}/pay",
    response_model=PayResponse,
    summary="Lancer un paiement pour une app (sans clé API)",
    responses={
        404: {"description": "Agrégateur inconnu ou app introuvable."},
        400: {"description": "Mode invalide."},
        422: {"description": "Réseau non supporté."},
        409: {"description": "Doublon ou mode replay sans template."},
        502: {"description": "Replay échoué et fallback navigateur désactivé."},
        503: {"description": "Service de paiement amont temporairement indisponible."},
    },
)
async def admin_pay(
    app_id: int,
    body: AdminPayRequest,
    debug: bool = Query(False, include_in_schema=False),
):
    """Exécute un paiement pour l'app `app_id` sans passer par une clé API.

    La transaction est rattachée à l'app (isolation) avec `api_key_id=None`
    pour traçabilité (lancé par admin). Même logique que `POST /pay`.
    """
    app = await tenants.get_app(app_id)
    if app is None:
        raise HTTPException(404, {"error": "app_not_found",
                                  "message": f"Aucune app trouvée avec l'id {app_id}."})

    from core.schemas.payments import PayRequest
    req = PayRequest(
        amount=body.amount,
        phone=body.phone,
        network=body.network,
        email=body.email,
        sender_name=body.sender_name,
        callback_url=body.callback_url or app.get("callback_url") or "",
        aggregator=body.aggregator,
        mode=body.mode,
        fallback_browser=body.fallback_browser,
        end_user_ref=body.end_user_ref,
    )
    return await _execute_payment(req, app_id=app_id, api_key_id=None, debug=debug)


@router.post(
    "/apps/{app_id}/payout",
    response_model=PayoutResponse,
    summary="Lancer un virement (payout) pour une app (sans clé API)",
    responses={
        404: {"description": "Agrégateur inconnu ou app introuvable."},
        409: {"description": "Un virement est déjà en cours vers ce bénéficiaire."},
        422: {"description": "Solde de l'app insuffisant."},
        502: {"description": "Le virement n'a pas pu être initié."},
        503: {"description": "Service de paiement amont temporairement indisponible."},
    },
)
async def admin_payout(
    app_id: int,
    body: AdminPayoutRequest,
    debug: bool = Query(False, include_in_schema=False),
):
    """Lance un virement pour l'app `app_id` sans clé API (débité de SON solde)."""
    app = await tenants.get_app(app_id)
    if app is None:
        raise HTTPException(404, {"error": "app_not_found",
                                  "message": f"Aucune app trouvée avec l'id {app_id}."})
    req = PayoutRequest(
        amount=body.amount,
        account_bank_code=body.account_bank_code,
        account_number=body.account_number,
        receiver_name=body.receiver_name,
        currency=body.currency,
        narration=body.narration,
        aggregator=body.aggregator,
        source_network=body.source_network,
        callback_url=body.callback_url or app.get("callback_url") or "",
        end_user_ref=body.end_user_ref,
    )
    return await _execute_payout(req, app_id=app_id, api_key_id=None, debug=debug)


@router.get("/apps/{app_id}/commission",
            summary="Lire la commission MW d'une app (None = défaut agrégateur)")
async def admin_get_app_commission(app_id: int):
    """Retourne la commission spécifique de l'app, ou null si elle utilise le défaut."""
    config = await db.get_app_commission(app_id)
    return {
        "app_id": app_id,
        "mw_commission_type": config["mw_commission_type"] if config else None,
        "mw_commission_value": config["mw_commission_value"] if config else None,
        "uses_default": config is None,
    }


@router.put("/apps/{app_id}/commission",
            summary="Définir une commission MW spécifique pour une app")
async def admin_set_app_commission(app_id: int, body: AppCommissionUpdate):
    """Pose ou efface la commission MW d'une app.

    - `mw_commission_type` + `mw_commission_value` non null → commission spécifique.
    - Les deux à null → revenir au défaut de l'agrégateur.
    Effet immédiat sur les prochains payins de cette app.
    """
    if body.mw_commission_type and body.mw_commission_type not in ("percent", "flat"):
        raise HTTPException(400, "mw_commission_type doit être 'percent' ou 'flat'")
    ok = await db.set_app_commission(
        app_id,
        mw_commission_type=body.mw_commission_type,
        mw_commission_value=body.mw_commission_value,
    )
    if not ok:
        raise HTTPException(503, "Supabase non configuré")
    return {"app_id": app_id, "mw_commission_type": body.mw_commission_type,
            "mw_commission_value": body.mw_commission_value,
            "uses_default": body.mw_commission_type is None}


@router.get("/apps/{app_id}/balance", summary="Solde courant d'une app")
async def admin_app_balance(app_id: int):
    """Solde réel de l'app (retirable) + sa réserve de frais de payout prépayés,
    et le total des deux. Vue admin : l'app elle-même ne voit que `balance`."""
    balance = await db.get_app_balance(app_id)
    reserve = await db.reserves(app_id)   # {agg: {réseau: montant au centime}}
    total_res = round(sum(sum(n.values()) for n in reserve.values()), 2)
    return {"app_id": app_id, "balance": balance,
            "by_aggregator": await db.balances_by_aggregator(app_id),
            "payout_fee_reserve": total_res,
            "payout_fee_reserve_by_network": {a: {k: round(v, 2) for k, v in n.items()}
                                              for a, n in reserve.items()},
            "total": round(balance + total_res, 2),
            "currency": "XAF"}


@router.get("/apps/{app_id}/ledger", summary="Grand livre détaillé d'une app (paginé)")
async def admin_app_ledger(app_id: int, page: int = Query(1, ge=1),
                           limit: int = Query(20, ge=1, le=100),
                           aggregator: str | None = Query(None),
                           start_date: str | None = Query(None, description="YYYY-MM-DD"),
                           end_date: str | None = Query(None, description="YYYY-MM-DD")):
    """Chaque mouvement du solde de l'app, plus récent d'abord, avec le détail
    figé du calcul : réseau, opération, brut, frais de dépôt / retrait /
    commission MobileWallet (montant XAF + pourcentage appliqué à ce moment-là)."""
    return _require_db(await db.list_app_ledger(app_id, page=page, limit=limit,
                                                aggregator=aggregator,
                                                start_date=start_date, end_date=end_date))


@router.post("/apps/{app_id}/credit", summary="Créditer manuellement le solde d'une app")
async def admin_app_credit(app_id: int, body: PlatformCredit):
    """Crédit MANUEL du solde d'une app (ajustement admin, sans payin rattaché).

    ⚠️ Crée de l'argent qui n'existe pas forcément chez DigiKUNTZ : peut introduire
    un écart visible dans `/admin/reconciliation`. À réserver aux ajustements/tests."""
    app = await tenants.get_app(app_id)
    if app is None:
        raise HTTPException(404, {"error": "app_not_found",
                                  "message": f"Aucune app trouvée avec l'id {app_id}."})
    _check_aggregator(body.aggregator)
    await db.credit_app_manual(app_id, body.amount, aggregator=body.aggregator,
                               currency=body.currency, reason="manual_admin",
                               balance_network=fee_network(body.network) or "orange")
    return {"app_id": app_id, "balance": await db.get_app_balance(app_id),
            "by_aggregator": await db.balances_by_aggregator(app_id),
            "currency": body.currency}


def _check_aggregator(name: str) -> None:
    if name not in registry.names():
        raise HTTPException(400, {"error": "unknown_aggregator",
                                  "message": f"Agrégateur inconnu : {name}."})


@router.post("/ledger/backfill", summary="Amorcer le ledger depuis l'historique des payins")
async def admin_ledger_backfill():
    """Crédite (idempotent) tous les payins 'successful' déjà en base qui n'ont pas
    encore de mouvement ledger. À lancer une fois pour initialiser les soldes."""
    return {"credited": await db.backfill_ledger_from_transactions()}


@router.get("/digikuntz/balance", summary="Solde du compte global DigiKUNTZ (trésorerie)")
async def admin_global_balance():
    """Solde RÉEL du compte global DigiKUNTZ (tous users/apps confondus).

    Donnée de trésorerie sensible → admin uniquement. Un client ne voit que le
    solde de son app (`GET /balance`). 503 si DigiKUNTZ injoignable."""
    from aggregators.digikuntz import status_poll
    bal = await status_poll.fetch_global_balance()
    if bal is None:
        raise HTTPException(503, {"error": "digikuntz_unavailable",
                                  "message": "Impossible de récupérer le solde DigiKUNTZ."})
    return bal


@router.get("/pawapay/balance", summary="Soldes des wallets pawaPay (trésorerie)")
async def admin_pawapay_balance():
    """Soldes RÉELS des wallets pawaPay (GET /v2/wallet-balances). Admin uniquement.
    503 si pawaPay injoignable."""
    from aggregators.pawapay import client
    balances = await client.fetch_balances()
    if balances is None:
        raise HTTPException(503, {"error": "pawapay_unavailable",
                                  "message": "Impossible de récupérer le solde pawaPay."})
    return {"balances": balances}


@router.get("/pawapay/config", summary="Configuration du compte pawaPay (opérateurs, min/max)")
async def admin_pawapay_config():
    """Configuration active du compte marchand pawaPay (GET /v2/active-conf) :
    opérateurs disponibles et montants min/max par opération (dépôt, payout,
    remboursement). Admin uniquement. 503 si pawaPay injoignable."""
    from aggregators.pawapay import client
    conf = await client.fetch_active_conf()
    if conf is None:
        raise HTTPException(503, {"error": "pawapay_unavailable",
                                  "message": "Impossible de récupérer la configuration pawaPay."})
    return conf


# TEMPORAIRE (diagnostic activation payout) : appels BRUTS à pawaPay, sans garde
# ni écriture en compta. Retourne le statut HTTP et le corps exacts. À retirer.
@router.post("/pawapay/raw-payout", summary="TEMP : POST /v2/payouts brut (diagnostic)")
async def admin_pawapay_raw_payout(amount: int, phone: str, network: str):
    import httpx
    from aggregators.pawapay import client
    body = client.payout_body(client.new_id(), amount, phone, network, "Test payout")
    async with httpx.AsyncClient(timeout=30) as http:
        r = await http.post(f"{client._pp.base.rstrip('/')}/v2/payouts",
                            json=body, headers=client._headers())
    try:
        resp = r.json()
    except ValueError:
        resp = r.text
    return {"http_status": r.status_code, "sent": body, "response": resp}


@router.get("/pawapay/raw-payout/{payout_id}", summary="TEMP : GET /v2/payouts/{id} brut")
async def admin_pawapay_raw_payout_status(payout_id: str):
    import httpx
    from aggregators.pawapay import client
    async with httpx.AsyncClient(timeout=15) as http:
        r = await http.get(f"{client._pp.base.rstrip('/')}/v2/payouts/{payout_id}",
                           headers=client._headers())
    try:
        resp = r.json()
    except ValueError:
        resp = r.text
    return {"http_status": r.status_code, "response": resp}


@router.get("/reconciliation", summary="Réconciliation par agrégateur : soldes internes vs soldes réels")
async def admin_reconciliation():
    """Pour CHAQUE agrégateur : Σ soldes apps + Σ réserves de frais des apps +
    solde plateforme doit égaler le solde réel chez lui. Un écart < 1 XAF
    (centimes d'arrondi) est normal ; il est reversé à la plateforme dès qu'il
    atteint 1 XAF (POST /admin/treasury/sweep, automatique après chaque règlement)."""
    return await treasury.reconciliation()


@router.post("/treasury/sweep", summary="Reverser le surplus d'arrondi à la plateforme")
async def admin_treasury_sweep():
    """Crédite à la plateforme les francs entiers de surplus d'arrondi (rien si
    une transaction est en vol). Déclenché aussi automatiquement."""
    return {"swept": await treasury.sweep_rounding_surplus()}


@router.get("/platform/balance", summary="Solde plateforme (argent propre de MobileWallet)")
async def admin_platform_balance():
    """Solde plateforme = marge/frais/flottant de MobileWallet. Peut être négatif
    (payouts plateforme non encore rechargés)."""
    split = await db.balances_by_aggregator()
    return {"balance": await db.get_platform_balance(),
            "by_aggregator": split["platform"], "currency": "XAF"}


@router.post("/platform/credit", summary="Recharger le solde plateforme")
async def admin_platform_credit(body: PlatformCredit):
    """Ajoute des fonds au solde plateforme (recharge manuelle)."""
    _check_aggregator(body.aggregator)
    await db.credit_platform(body.amount, reason="reload", currency=body.currency,
                             aggregator=body.aggregator)
    return {"balance": await db.get_platform_balance(), "currency": body.currency}


@router.post(
    "/platform/payout",
    response_model=PayoutResponse,
    summary="Virement débité du solde PLATEFORME (pas d'une app)",
    responses={
        400: {"description": "Agrégateur sans support payout."},
        409: {"description": "Un virement est déjà en cours vers ce bénéficiaire."},
        502: {"description": "Le virement n'a pas pu être initié."},
        503: {"description": "Service de paiement amont temporairement indisponible."},
    },
)
async def admin_platform_payout(
    body: AdminPayoutRequest,
    debug: bool = Query(False, include_in_schema=False),
):
    """Virement sortant débité du solde PLATEFORME (argent propre de MobileWallet).

    PAS de contrôle de solde (le solde peut devenir négatif → recharger via
    `POST /admin/platform/credit`). Suivi identique : polling auto + webhook."""
    req = PayoutRequest(
        amount=body.amount, account_bank_code=body.account_bank_code,
        account_number=body.account_number, receiver_name=body.receiver_name,
        currency=body.currency, narration=body.narration,
        aggregator=body.aggregator, callback_url=body.callback_url,
        end_user_ref=body.end_user_ref,
    )
    return await execute_platform_payout(req, debug=debug)


# --- Config des commissions par agrégateur ---

@router.get("/aggregators/{name}/fees",
            summary="Lire la config de commission d'un agrégateur")
async def admin_get_aggregator_fees(name: str):
    """Retourne la config complète (taux agrégateur + commission MW) d'un agrégateur."""
    config = await db.get_aggregator_config(name)
    if config is None:
        raise HTTPException(404, f"Agrégateur '{name}' absent de la BD (migration 019 appliquée ?)")
    return config


@router.put("/aggregators/{name}/fees",
            summary="Modifier la config de commission d'un agrégateur")
async def admin_update_aggregator_fees(name: str, body: AggregatorConfigUpdate):
    """Crée ou met à jour les taux de commission pour un agrégateur.

    - `aggregator_fee_rate` : taux prélevé par l'agrégateur (ex. 0.05 = 5%).
    - `mw_commission_type` + `mw_commission_value` : commission MobileWallet sur le net.
      Type `percent` (ex. 0.05 = 5%) ou `flat` (montant fixe XAF, ex. 500).

    Effet immédiat sur les prochains payins : aucun redémarrage nécessaire.
    """
    if body.mw_commission_type and body.mw_commission_type not in ("percent", "flat"):
        raise HTTPException(400, "mw_commission_type doit être 'percent' ou 'flat'")
    if body.fee_payer and body.fee_payer not in FEE_PAYERS:
        raise HTTPException(400, f"fee_payer doit être l'un de {FEE_PAYERS}")
    row = await db.upsert_aggregator_config(
        name,
        display_name=body.display_name,
        aggregator_fee_rate=body.aggregator_fee_rate,
        mw_commission_type=body.mw_commission_type,
        mw_commission_value=body.mw_commission_value,
        active=body.active,
        fee_payer=body.fee_payer,
    )
    if row is None:
        raise HTTPException(503, "Supabase non configuré")
    return row
