# ARCHITECTURE — MobileWallet backend (vision 360)

> **À lire en début de session** pour connaître le projet sans tout parcourir.
> Si tu modifies la structure (nouveau fichier/module/endpoint/table) ou un flux,
> **mets ce fichier à jour** (ainsi que le README) avant de clore le travail.

## En une phrase

Backend FastAPI qui exécute des paiements **Mobile Money** (XAF, Cameroun) via des
**agrégateurs** (DigiKUNTZ aujourd'hui). Deux moteurs par agrégateur : un **navigateur
IA** (Playwright + LLM DeepSeek qui pilote le checkout Flutterwave) et un **curl replay**
(rejoue la requête déduite, sans navigateur).

## Principe directeur (NON négociable — cf. CLAUDE.md)

**L'IA décide, le code informe.** Le backend fournit à l'IA un snapshot fidèle + des
outils + un objectif ; il ne décide JAMAIS à sa place qu'un paiement a réussi/échoué.
Seule exception : les faits opérateur universels et mécaniques (un échec verify après USSD → `cancelled`).

---

## Carte des fichiers

```
ai_browser2/
├── main.py                       Lanceur uvicorn (importe core.server:app). RELOAD=1 = hot-reload.
├── start.sh / dev.sh             Démarrage prod (sans reload) / dev (avec reload).
│                                 ⚠️ Tester /pay avec start.sh : le hot-reload coupe les requêtes longues.
│
├── core/                         === MOTEUR GÉNÉRIQUE (agnostique de l'agrégateur) ===
│   ├── server.py                 ASSEMBLAGE FastAPI seulement : crée l'app, lifespan (pool
│   │                             navigateur + LLM dans runtime), monte les routers. Pas de logique.
│   ├── background_jobs.py        Démarre au lancement `aggregators.<nom>.background.start()` s'il existe.
│   ├── runtime.py                État partagé entre routers (browser/llm vivants) + helpers
│   │                             transverses (seconds_since, fmt_duration).
│   ├── routers/                  UN APIRouter PAR DOMAINE (logique des endpoints) :
│   │   ├── availability.py       GET /availability (état des opérateurs), GET /availability/status-page (page de statut de l'agrégateur), GET /pay/minimum (min/max d'un dépôt) + POST /operators/predict.
│   │   ├── admin_statements.py   POST /admin/{agg}/statements/sync + GET /admin/{agg}/statements (paginé).
│   │   ├── payout_ops.py         POST /payout/bulk (≤20, un appel agrégateur) + POST /payout/{ref}/cancel.
│   │   ├── payment_page.py       POST /pay/page : page de paiement hébergée -> redirect_url.
│   │   ├── system.py             /health, /aggregators, /config/max-tabs.
│   │   ├── payments.py           /pay (dispatch, mock, chemin navigateur/replay, chemin « API
│   │   │                         directe » `_execute_direct` pour les agrégateurs direct_api).
│   │   ├── payments_common.py    Helpers /pay : garde anti-doublon, _settle, split payin, rendu.
│   │   │                         Court-circuité par le mode mock (settings.mock_payments).
│   │   │                         Crédite le solde de l'app (app_ledger) au succès payin.
│   │   ├── payout.py             /payout (virement sortant) : garde anti-doublon par
│   │   │                         bénéficiaire, RÉSERVE atomique du solde (db.reserve_payout),
│   │   │                         initiate_payout, webhook (+ poll DigiKUNTZ) -> verdict, REMBOURSE si échec.
│   │   │                         GET /payout/{ref}/check : 1 appel statut agrégateur à la demande.
│   │   ├── transactions.py       /transactions*, /status/{ref}, /status/{ref}/check, /cancel, /balance
│   │   │                         (scopés à l'app).
│   │   ├── templates.py          /aggregators/{name}/template (consulter/poser le replay).
│   │   ├── webhooks.py           /webhook/digikuntz + /webhook/pawapay (callbacks statut entrants,
│   │   │                         règlement commun `_settle` : BD + ledger + notification).
│   │   ├── auth.py               /signup,/login,/refresh,/logout : compte dev (self-service).
│   │   ├── projects.py           /apps,/apps/{id}/keys : le dev gère SES apps/clés (require_dev).
│   │   ├── admin.py              /admin/* : supervision developers/apps/clés + /admin/apps/{app_id}/pay
│   │   │                         + /admin/apps/{app_id}/payout + .../balance + /ledger/backfill
│   │   │                         + /digikuntz/balance (solde global) + /pawapay/balance
│   │   │                         + /pawapay/config (opérateurs actifs, montants min/max)
│   │   │                         + /reconciliation (require_admin).
│   │   └── dev.py                /drive, /test-llm.
│   ├── schemas/                  Modèles Pydantic par domaine (payments, operations, system, templates, dev,
│   │                             admin, auth, projects).
│   ├── auth.py                   Auth des CLÉS API d'app (paiements) : génération/hash des clés,
│   │                             AuthContext, cache TTL, dépendances require_api_key / require_admin.
│   ├── dev_auth.py               Auth du COMPTE developer (self-service) : bcrypt + JWT access/
│   │                             refresh, dépendance require_dev, DevContext.
│   ├── tenants.py                Persistance multi-tenant (developers/apps/api_keys + comptes/
│   │                             refresh) : résolution de clé (vue api_key_context), CRUD,
│   │                             ownership (app_belongs_to), isolation, webhook_deliveries.
│   ├── notifications.py          Verdict -> client : webhook sortant signé (HMAC) vers le
│   │                             callback_url + push Socket.IO. Non bloquant, idempotent, retries.
│   ├── realtime.py               Serveur Socket.IO (monté en ASGI sur FastAPI) : auth de la
│   │                             connexion par clé API, rooms app:<id>/dev:<id>, émission verdict.
│   ├── base.py                   Contrat `Aggregator` (ABC) + dataclasses PaymentRequest /
│   │                             PaymentResult (porte error_code, curl_template, errors…) / CurlTemplate.
│   ├── registry.py               Registre nom -> instance d'agrégateur (register / get / names).
│   ├── verdict_registry.py       Boîte aux lettres webhook⇄polling par id provider (une instance
│   │                             par agrégateur : le premier à voir le terminal réveille l'autre).
│   ├── mock_aggregator.py        Mode mock (MOCK_PAYMENTS=true) : simule un paiement sans appeler
│   │                             DigiKUNTZ (ussd_sent immédiat puis successful/cancelled après délai).
│   │                             Socket.IO + webhook se déclenchent normalement. Utile si DigiKUNTZ down.
│   ├── config.py                 settings centralisés depuis .env (DigiKUNTZ, FLW, LLM, Supabase,
│   │                             retry_window_* par réseau, max_tabs_per_browser, mock_payments,
│   │                             digikuntz.use_callback).
│   ├── db.py                     Couche Supabase async (no-op si non configuré) : transactions (audit, statut, annulation) ;
│   │                             (core/db_diagnostics.py, core/db_config.py, core/db_ledger.py en mixins).
│   ├── db_diagnostics.py         DiagnosticsMixin (hérité par Database) : transaction_errors, traces IA.
│   ├── db_reserve.py             ReserveMixin : réserve de frais de payout PAR APP et PAR RÉSEAU, au centime (app_reserve_ledger, RPC reserve_payout_with_fee).
│   ├── db_config.py              ConfigMixin (hérité par Database) : réglages app_settings, templates curl.
│   ├── db_ledger.py              LedgerMixin (hérité par Database) : comptabilité —
│   │                             app_ledger (credit_app/get_app_balance/reserve_payout/backfill/total),
│   │                             platform_ledger (credit_platform/debit_platform/get_platform_balance),
│   │                             aggregators (get/list/upsert_aggregator_config),
│   │                             payouts (insert_pending_payout/last_payout_for_account/update_payout).
│   ├── db_ledger_details.py      LedgerDetailsMixin : détail figé du calcul sur app_ledger (réseau, brut,
│   │                             frais dépôt/retrait/commission + pourcentages, migration 027) + lecture paginée.
│   ├── fees.py                   Calcul PUR de ventilation (compute_fees / fees_info / fee_payer) :
│   │                             brut → frais agrégateur (selon fee_payer) → commission MW → app + plateforme.
│   ├── aggregator_ops.py         Accès neutre aux opérations optionnelles (aggregators.<nom>.operations).
│   ├── openapi_docs.py           Swagger : routes communes (/openapi/common.json) + onglet par agrégateur (/openapi/<nom>.json) + section admin seulement avec AdminKey valide (/openapi/admin.json), page /docs.
│   ├── availability.py           Dispo opérateurs (neutre) : list + guard 503 sur /pay et /payout si CLOSED, 400 si NOT_ENABLED (DELAYED = payout en file).
│   ├── deposit_limits.py         Min/max d'un dépôt (neutre) : notre min (reste >= 1 F à l'app après frais, commission, réserve) et celui de l'agrégateur ; 422 sur /pay et /pay/page.
│   ├── status_check.py           Vérif. manuelle chez l'agrégateur (1 appel, payin/payout) -> settle_verdict.
│   ├── treasury.py               Soldes réels, réconciliation, reversement auto (réserve libérée + surplus d'arrondi) à la plateforme.
│   ├── settlement.py             Imputation en compta (commune à tous les agrégateurs) : credit_payin,
│   │                             payout_charges, refund_payout, settle_verdict (verdict terminal -> BD + compta + notif).
│   │                             Résout taux + fee_payer en BD.
│   ├── browser/                  Couche navigateur Playwright (importée via `core.browser`) :
│   │   ├── __init__.py           Ré-exporte CapturedRequest, DomSnapshot, BrowserSession, BrowserController.
│   │   ├── models.py             CapturedRequest (capture réseau, to_curl) + DomSnapshot.
│   │   ├── session.py            BrowserSession (1 transaction = 1 contexte isolé) : état + open/close.
│   │   ├── diagnostics.py        Mixin : page instrumentée, console/réseau, requêtes en vol,
│   │   │                         signaux d'erreur, watch/wait_for_page_change.
│   │   ├── capture.py            Mixin : capture réseau (hook crypto, requêtes charge/verify).
│   │   ├── actions.py            Mixin : goto/reload, iframe, snapshot DOM, click/fill/select/scroll/wait.
│   │   └── controller.py         BrowserController (POOL : acquire/release_session, N sessions par
│   │                             Chrome puis 2e Chrome).
│   ├── browser_runner.py         Orchestration navigateur générique : acquiert une session, crée
│   │                             la transaction, navigue, lance ReasoningLoop, délègue le verdict à
│   │                             l'agrégateur, capture charge/verify, construit le curl_template.
│   ├── reasoning_loop.py         Boucle IA : snapshot -> prompt -> LLM -> actions. Gère await_change
│   │                             (attente passive bornée par le TEMPS, pas par max_turns), plafond
│   │                             garde-fou BROWSER_LOOP_MAX_S (deadline_exceeded dans le header).
│   ├── llm_client.py             Client DeepSeek (LlmClient.send).
│   ├── classifier.py             Classification statut par mots-clés (apprend de nouveaux mots).
│   ├── error_tracking.py         build_errors() : dérive les lignes transaction_errors (engine + source).
│   ├── upstream_errors.py        Détection panne amont -> codes network_unavailable /
│   │                             operator_unavailable + messages FR (réponse 503 propre au client).
│   └── crypto/                   encrypt.js (cryptico via Node) + cryptico_py.py — chiffrement charge.
│
├── aggregators/                  === UN DOSSIER PAR AGRÉGATEUR ===
│   └── digikuntz/
│       ├── status_check.py       check_deposit_status : 1 appel statut DigiKUNTZ à la demande.
│       ├── aggregator.py         DigikuntzAggregator : implémente l'ABC, s'enregistre, replay (steps
│       │                         + ReplayConfig par appel), extract_curl_template, matchers.
│       ├── browser_flow.py       Mode navigateur : browser_objective (prompt FR — le navigateur
│       │                         S'ARRÊTE dès l'USSD demandé), decide_browser_outcome (pose
│       │                         poll_after_close, tab fermée), finalize_after_close (polling
│       │                         verify HORS navigateur), _interpret/_friendly.
│       ├── checkout_matchers.py  Reconnaît les requêtes /charge et /verify du checkout Flutterwave
│       │                         embarqué par DigiKUNTZ (hors de core/, qui reste neutre).
│       ├── status_poll.py        Polling statut DigiKUNTZ (GET {base}/transaction?transactionId=) :
│       │                         source de vérité du verdict après USSD. payin_* -> interne, poll
│       │                         jusqu'à un verdict terminal (sans timeout : l'opérateur tranche toujours). 0 token.
│       │                         poll_transaction : boucle GET /transaction + registre webhook (payout + charge directe).
│       ├── direct_charge.py      Charge directe (mode `direct` de /pay) : POST {base}/direct-charge -> USSD
│       │                         poussé sans navigateur ; verdict via poll_transaction. En plus du navigateur.
│       ├── replay_flow.py        Façade du mode curl replay : ré-exporte replay/ (imports `replay_flow.X`).
│       ├── replay/               Mode curl replay (sans navigateur), découpé par rôle :
│       │   ├── interpret.py      Codes Flutterwave -> (statut, message) : interpret_charge/verify/ping, libellés réseau/USSD.
│       │   ├── config.py         Constantes DigiKUNTZ/Flutterwave (.env), HEADERS, ReplayConfig (config par appel).
│       │   ├── steps.py          step1..step4 (create/init/charge/poll verify), appels HTTP.
│       │   └── cli.py            Point d'entrée CLI de test manuel (enchaîne step1..step4).
│       └── payout_flow.py        Payout (retrait) : initiate_payout (POST {base}/payout) +
│                                 poll_payout_status (GET {base}/transaction, mapping payout_*,
│                                 registre webhook⇄polling). Aucun navigateur/LLM/Flutterwave.
│   └── pawapay/                  Agrégateur « API directe » (Merchant API v2, Bearer token) :
│       ├── operations.py         predict-provider, resend-callback, fail-enqueued, payouts/bulk, paymentpage.
│       ├── availability.py       GET /v2/availability (cache 60 s) -> état par réseau/opération, NOT_ENABLED si non activé sur le compte. Seule source du blocage (la page de statut ne bloque pas).
│       ├── statements.py         Relevés (POST /v2/statements -> CSV) regroupés en 1 ligne par transaction dans
│       │                         pawapay_statement_transactions ; synchro auto (PAWAPAY_STATEMENT_SYNC day|week|month, recouvrement 2 j).
│       ├── background.py         Tâches de fond pawaPay (synchro des relevés), lancées par core/background_jobs.
│       ├── status_page.py        status.pawapay.io (API publique, cache 60 s) : état brut pour /availability/status-page, INFORMATIF (ne bloque aucun paiement).
│       ├── account_conf.py       GET /v2/active-conf (cache 10 min) : opérations activées sur le compte, min/max, code USSD de secours.
│       ├── status_check.py       check_deposit_status : 1 appel GET /v2/deposits/{id} à la demande.
│       ├── client.py             HTTP pawaPay : initiate_deposit/payout, fetch_status, soldes,
│       │                         mapping statuts (COMPLETED/FAILED/… -> interne), MSISDN 237…,
│       │                         providers MTN_MOMO_CMR / ORANGE_CMR, poll_until_final (sans
│       │                         timeout, écoute le webhook via son VerdictRegistry) ; wait_webhook (payouts :
│       │                         webhook seul, aucun polling).
│       ├── aggregator.py         PawapayAggregator (direct_api=True) : pay_direct -> ussd_sent +
│       │                         poll_after_close ; finalize_after_close pose le statut final.
│       │                         Hooks navigateur/replay du contrat = NotImplementedError.
│       └── payout_flow.py        Payout pawaPay, même interface que digikuntz.payout_flow.
│
├── schema/
│   ├── supabase.sql              Schéma de base : transactions, curl_templates (-> digikuntz_curl_templates, 029).
│   └── migrations/               Évolutions idempotentes, une par fichier :
│       ├── 001_transaction_traces.sql   Trace tour-par-tour de l'IA.
│       ├── 002_app_settings.sql         Réglages clé/valeur (ex. max_tabs_per_browser).
│       ├── 003_transaction_errors.sql   Erreurs détaillées par moteur + source.
│       ├── 004_provider_transaction_id.sql  Id provider (webhook/polling).
│       ├── 005_cancelled_at.sql        Horodate le passage à 'cancelled' (audit).
│       ├── 006_ussd_sent_at.sql        Horodate l'envoi USSD (anti-doublon cancelled).
│       ├── 007_validated_at.sql        Horodate la validation USSD (anti-doublon après succès).
│       ├── 008_settled_by.sql          Qui a settlé le verdict : 'polling' | 'webhook'.
│       ├── 009_multitenant_tables.sql  developers/apps/api_keys + vue api_key_context.
│       ├── 010_transactions_tenant_columns.sql  transactions.app_id/api_key_id/end_user_ref.
│       ├── 011_webhook_deliveries.sql  Journal d'envoi webhook (idempotence (tx,event)).
│       ├── 012_developers_auth.sql  developers.password_hash / email_verified (compte dev).
│       ├── 013_refresh_tokens.sql  Sessions dev : refresh tokens hashés (rotation/logout).
│       ├── 014_curl_templates_status.sql  Fiabilité d'un template (untested/working/failed) :
│       │                                  le replay réutilise le dernier 'working', jamais un 'failed'.
│       ├── 015_app_ledger.sql      Grand livre par app (credit/debit) + vue app_balance.
│       │                           Unique (transaction_id, direction) = idempotence du solde.
│       ├── 016_reserve_payout.sql  Fonction RPC atomique : contrôle solde + insertion débit
│       │                           sous advisory lock par app (anti double-dépense).
│       ├── 017_transactions_payout.sql  transactions.type ('payin'|'payout') + colonnes payout.
│       ├── 018_platform_ledger.sql  Solde PLATEFORME (argent propre MobileWallet) + vue platform_balance.
│       └── 019_aggregators.sql      Config des taux par agrégateur : aggregator_fee_rate (5% DigiKUNTZ),
│                                    mw_commission_type/value (percent|flat). Pré-rempli digikuntz 5%/5%.
│       ├── 022_aggregator_pawapay.sql  Ligne 'pawapay' (commission MW à 0, à ajuster).
│       ├── 023_aggregator_fee_payer.sql aggregators.fee_payer, table aggregator_fee_rates
│       │                               (taux par réseau/opération), transactions.provider_fee.
│       ├── 024_ledger_by_aggregator.sql Soldes par agrégateur : colonne aggregator sur
│       │                               app_ledger/platform_ledger (trigger), vues *_by_aggregator,
│       │                               reserve_payout contrôle le solde chez l'agrégateur du payout.
│       ├── 025_transaction_callback_url.sql / 026_app_payout_reserve.sql (cf. en-têtes).
│       ├── 027_ledger_fee_details.sql  app_ledger : détail figé du calcul (brut, réseau,
│       │                               frais dépôt/retrait/commission + pourcentages).
│       ├── 028_pawapay_statements.sql  pawapay_statement_syncs (journal des synchros).
│       ├── 029_digikuntz_curl_templates.sql  curl_templates renommée digikuntz_curl_templates.
│       ├── 030_pawapay_statement_transactions.sql  relevé pawaPay : 1 ligne par transaction,
│                                       frais pawaPay et frais opérateur en colonnes.
│       └── 031_ledger_zero_credit.sql  app_ledger.amount >= 0 : payin absorbé par les frais visible (crédit 0).
│
├── docs/openapi.json             Swagger versionné (régénérer via scripts/dump_openapi.py).
├── fly.toml                      Déploiement Fly.io : machine toujours allumée (auto_stop off).
├── .github/workflows/            Actions manuelles : fly-on.yml (allumer, permanent) /
│                                 fly-off.yml (éteindre complètement). Secret FLY_API_TOKEN.
├── scripts/dump_openapi.py       Dump du schéma OpenAPI.
├── todo/                         Chantiers planifiés (migration vue admin, fallback poll DigiKUNTZ).
├── README.md                     Doc d'usage (install, endpoints, modes, persistance).
├── ARCHITECTURE.md               CE fichier (vision 360).
└── CLAUDE.md                     Règles projet (branches, philosophie IA, secrets, swagger, BD).
```

---

## Le flux d'un paiement `POST /pay`

1. **server.py** valide (agrégateur, réseau), applique la **garde anti-doublon** par numéro :
   - dernière transaction `pending` → 409 (confirmer/annuler) ;
   - dernière `ussd_sent` (PIN pas encore tranché) → 409 pending_exists tant que la
     demande n'a pas expiré côté opérateur (17 min depuis `ussd_sent_at`) ;
   - dernière `cancelled` ou `failed` APRÈS envoi USSD (depuis `ussd_sent_at`), ou
     `successful` (depuis `validated_at`, délai court RETRY_WINDOW_SUCCESS_S = 1 min
     tous réseaux) dans le délai anti-doublon → 409 retry_too_soon ;
   - refus immédiat (`failed` sans USSD envoyé) / panne → relançable tout de suite.
2. Insère la transaction `pending`, dispatch selon `mode` (auto/browser/replay, ou `direct` -> `pay_direct` sans navigateur
   pour les agrégateurs `supports_direct`) vers
   l'agrégateur (`pay_via_browser` ou `replay`).
3. **browser** : `run_browser_flow` acquiert une **session isolée**, crée la transaction
   DigiKUNTZ, ouvre le checkout Flutterwave, lance la **boucle IA** (remplir + Payer). Dès
   que l'USSD est demandé au client, **le navigateur S'ARRÊTE** (conclut `ussd_sent`, ~6
   tours — pas de surveillance coûteuse de l'écran). `decide_browser_outcome` extrait alors
   les params verify (flw_ref/modalauditid) et les pose sur `result.poll_after_close` SANS
   poller : **la tab est fermée immédiatement** (`release_session`). Ce n'est qu'APRÈS, hors
   session, que `run_browser_flow` appelle `finalize_after_close` -> **polling verify
   Flutterwave** (`status_poll.py`) en HTTP pur, jusqu'au verdict terminal (sans timeout :
   échec après USSD -> `cancelled`). Aucun navigateur n'est tenu pendant l'attente ; le polling ne
   coûte aucun token.
   *(Webhook serveur possible quand le backend a une URL publique — cf.
   `todo/webhook-digikuntz.md`.)*
4. **replay** : `step1..step4` rejouent charge + poll verify (sans timeout), `ReplayConfig` par appel.
5. **finally** : settle la transaction en BD (+ trace + errors), construit la réponse.
   - panne amont (API/opérateur) → **503** `{code, message}` (vue uniforme client) ;
   - sinon **vue client** minimale (success/status/message/transaction_id/code) ; `?debug=true`
     ajoute le détail technique (admin).

## Statuts finaux possibles

- `successful` — payé.
- `failed` — échec AVANT l'USSD (solde insuffisant au /charge, etc.). Relançable.
- `cancelled` — échec APRÈS l'USSD = refus/non-validation, par l'utilisateur OU
  l'opérateur (un solde insuffisant ne déclenche jamais d'USSD). Le polling tourne
  sans timeout jusqu'à ce verdict (l'opérateur tranche toujours). **Bloque** le
  numéro pendant le délai anti-doublon.
- `pending` — en cours.
- `network_unavailable` / `operator_unavailable` — pannes amont → **503**.

(Plus de statut `expired` : le polling n'a plus de timeout, il attend le verdict
terminal de l'opérateur, qui devient `cancelled` en cas d'échec après USSD.)

**Délai anti-doublon DÉPEND du réseau**, réglable par env
(`settings.retry_window_for(network)`, env `RETRY_WINDOW_ORANGE_S`/`_MTN_S`).
Sert UNIQUEMENT à la garde anti-doublon — il ne borne plus le polling.
`pending`, `cancelled` ET `successful` bloquent un nouveau paiement pendant cette
fenêtre ; le temps restant ("Réessayez dans X") = `retry_window(réseau) − écoulé`,
l'écoulé étant compté depuis :
- `cancelled` → l'ENVOI de l'USSD (`ussd_sent_at`, fallbacks `cancelled_at` puis
  `created_at`) ;
- `successful` → la VALIDATION de l'USSD (`validated_at`, fallback `created_at`),
  message « Vous avez récemment effectué un paiement… ».

## Comptabilité multi-tenant : soldes & retraits (payout)

**MobileWallet est lui-même un agrégateur.** Côté DigiKUNTZ il n'existe qu'**un
seul compte global** où atterrit l'argent de TOUTES les apps de tous les users
(tous les payins). DigiKUNTZ ne connaît pas nos apps : c'est un pot commun unique.
C'est donc à **nous** de tenir le **solde de chaque app**.

- **Grand livre (`app_ledger`)** : une ligne par mouvement — `credit` (payin réussi
  de l'app) ou `debit` (payout de l'app). Le **solde** d'une app = `SUM(credit) −
  SUM(debit)` (vue `app_balance`). Source de vérité unique, auditable.
- **Invariant comptable** : `Σ soldes des apps == solde réel du compte global
  DigiKUNTZ`. Une app ne peut JAMAIS retirer plus qu'elle n'a encaissé (sinon elle
  retirerait l'argent d'une autre). Le solde global réel se lit côté agrégateur via
  `status_poll.fetch_global_balance()` (GET `{base}/balance`) ; `GET /admin/reconciliation`
  compare `Σ soldes apps` (db.total_apps_balance) à ce solde global et signale tout écart.
  Le solde global est une donnée de trésorerie **réservée à l'admin** (un client ne
  voit que le solde de son app via `GET /balance`).
- **Solde plateforme (`platform_ledger`)** : l'argent PROPRE de MobileWallet
  (marge/frais/flottant), distinct des apps clientes. L'admin le recharge
  (`POST /admin/platform/credit`) et retire dessus (`POST /admin/platform/payout`)
  **sans contrôle de solde** (peut devenir négatif). L'invariant complet est donc
  **par agrégateur** : `Σ soldes apps + solde plateforme (portion de l'agrégateur) ==
  solde réel chez cet agrégateur` (compte global DigiKUNTZ, wallets pawaPay ; cf.
  `/admin/reconciliation`). Chaque écriture de ledger porte son agrégateur (migration
  024) : un écart ou des tests sur l'un n'affectent pas les autres, et un payout ne
  dépense que le solde de l'app chez l'agrégateur du payout.
- **Taux de commission par agrégateur** : stockés en BD (`aggregators`, migration 019),
  modifiables à chaud via `PUT /admin/aggregators/{name}/fees`. Deux taux :
  - `aggregator_fee_rate` : ce que prend l'agrégateur sur le brut (DigiKUNTZ = 5%) ;
  - `mw_commission_type/value` : commission MobileWallet sur le net (`percent` ou `flat`).
  Consultables publiquement via `GET /aggregators` et `GET /aggregators/{name}/fees`
  (avec simulation pour un montant donné).
- **Qui paie les frais de l'agrégateur** (`aggregators.fee_payer`, migration 023) :
  `customer` (pris au client final, ex. DigiKUNTZ : hors compta), `app` (pris sur le
  wallet marchand et déduits de l'app, ex. pawaPay) ou `platform` (supportés par MW).
  Taux par réseau/opération dans `aggregator_fee_rates` ; frais = ceil(brut × taux),
  enregistrés dans `transactions.provider_fee`.
- **Split comptable au payin** (`core/settlement.py` → `credit_payin`, calcul dans
  `core/fees.py`) : app = brut − frais(si payer=app) − commission ; plateforme =
  commission (− frais si payer=platform). Invariant : app + plateforme = ce que le
  wallet a réellement reçu. Idempotent (même `transaction_id`), donc aucun double
  crédit même si polling et webhook concourent.
- **Frais de retrait prépayés** (`aggregators.prepay_payout_fee`, pawaPay) : au dépôt,
  l'app est créditée de B tel que B + frais_retrait(B) = reste ; la réserve va au
  solde plateforme, qui paie ensuite les frais du payout. Retirer B ne coûte donc
  plus rien à l'app.
- **Payout** : débit de l'app = montant (+ frais si payer=app sans prépaiement) ;
  remboursement identique si le virement échoue (`settlement.refund_payout`).
- **Crédit au payin** : le split est appliqué quand un payin devient `successful`
  (settle polling OU webhook). `db.backfill_ledger_from_transactions()` (endpoint
  `POST /admin/ledger/backfill`) amorce les soldes depuis l'historique (sans split,
  montant brut — les anciens payins antérieurs à la migration 019).

### Flux d'un retrait `POST /payout`
1. Garde anti-doublon par bénéficiaire (un seul payout `pending` par
   `account_number`).
2. Insère la transaction `pending` (`type='payout'`).
3. **RÉSERVE atomique** du solde : `db.reserve_payout(app_id, amount, tx_id)` appelle
   la RPC Postgres (advisory lock par app) qui contrôle le solde ET insère le débit
   dans la même transaction. Solde insuffisant → **422** (aucun appel DigiKUNTZ).
4. `initiate_payout` (`POST {base}/payout`). Panne amont → **503** + remboursement
   du débit ; refus immédiat → `failed` + remboursement.
5. Réponse immédiate `pending` ; `poll_payout_status` (hors requête) attend le
   verdict terminal (DigiKUNTZ : registre webhook⇄polling ; pawaPay : webhook, avec
   filet de sécurité `GET /v2/payouts/{id}` à 30 s, 90 s, 5 min, 15 min puis
   toutes les heures si le webhook n'arrive pas). `GET /payout/{ref}/check`
   (ref = `transaction_ref` ou `provider_transaction_id`) interroge l'agrégateur UNE fois à la
   demande et applique le verdict s'il est final (`settlement.settle_verdict`,
   `settled_by = manual_check`).
6. Verdict : `successful` → le débit reste ; `failed`/`cancelled` → **remboursement**
   (crédit compensatoire idempotent, reason `payout_refund`). Verdict poussé au
   client via webhook signé + Socket.IO ; consultable par `GET /status/{ref}`.

> Règle : on **réserve (débite) à l'initiation** et on **rembourse si le retrait
> échoue** → un payout en vol bloque déjà les fonds (pas de double-dépense) et
> l'invariant tient à tout instant. Les statuts `payout_*` sont des faits opérateur
> mécaniques (exception CLAUDE.md autorisée), pas un verdict déduit par le code.

## Concurrence

- **Navigateur** : 1 `BrowserSession` = 1 `BrowserContext` isolé (cookies/onglets séparés) ;
  fermer une session n'affecte jamais les autres. Pool : `max_tabs` sessions par Chrome,
  puis un nouveau Chrome. Seuil réglable (env `MAX_TABS_PER_BROWSER` + BD app_settings +
  API `PUT /config/max-tabs`).
- **Replay** : `ReplayConfig` construit par appel (aucun global de module muté) → N replays
  parallèles sans interférence.

## Points d'entrée de code utiles

- Ajouter un agrégateur → `core/base.py` (l'ABC) + `aggregators/<nom>/` (API pure : `direct_api = True`
  + `pay_direct`/`finalize_after_close`, cf. pawaPay).
- Toucher la décision de l'IA → `core/reasoning_loop.py` + `aggregators/*/browser_flow.py`
  (objectif + decide_browser_outcome). **Ne jamais y remettre de "code qui décide".**
- Toucher la persistance → `core/db.py` (+ une migration dans `schema/migrations/`).
- Toucher les endpoints/modèles → `core/server.py` puis régénérer `docs/openapi.json`.


## Frais et réserves (migration 026)

Dépôt réussi (fee_payer=app, prepay_payout_fee) : frais agrégateur et commission
MobileWallet arrondis à l'excès ; ceil(brut x taux de payout du réseau du dépôt)
va dans la **réserve de l'app pour ce réseau** (app_reserve_ledger, au centime) ;
le reste sur le solde de l'app (francs entiers).

Payout de X vers le réseau n (SQL `reserve_payout_with_fee`, atomique) :
- partie couverte par la réserve n : frais EXACTS (centimes) pris sur la réserve,
  qui couvre donc toujours, quelle que soit la façon de retirer ;
- partie venue d'un autre réseau m : frais arrondis à l'excès pris sur le solde,
  et la réserve m correspondante est libérée vers la plateforme ;
- solde vidé : la réserve restante est libérée ;
- échec : chaque débit (solde, réserves) est rendu.

core/treasury.py reverse à la plateforme, en francs entiers et quand rien n'est en
vol, la réserve libérée puis le surplus d'arrondi (écart réel - interne, < 10 XAF).
`GET /payout/quote` donne au client les frais pris sur son solde avant d'appeler
`POST /payout` (qui renvoie aussi `fee` et `total_debited`). `GET /balance` (app)
= solde retirable ; `GET /admin/apps/{id}/balance` = solde + réserves par réseau + total.
