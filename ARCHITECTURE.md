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
│   ├── runtime.py                État partagé entre routers (browser/llm vivants) + helpers
│   │                             transverses (seconds_since, fmt_duration).
│   ├── routers/                  UN APIRouter PAR DOMAINE (logique des endpoints) :
│   │   ├── system.py             /health, /aggregators, /config/max-tabs.
│   │   ├── payments.py           /pay (dispatch, garde anti-doublon, settle, vues client/debug).
│   │   │                         Court-circuité par le mode mock (settings.mock_payments).
│   │   │                         Crédite le solde de l'app (app_ledger) au succès payin.
│   │   ├── payout.py             /payout (virement sortant) : garde anti-doublon par
│   │   │                         bénéficiaire, RÉSERVE atomique du solde (db.reserve_payout),
│   │   │                         initiate_payout, poll/webhook -> verdict, REMBOURSE si échec.
│   │   ├── transactions.py       /transactions*, /status/{ref}, /cancel, /balance (scopés à l'app).
│   │   ├── templates.py          /aggregators/{name}/template (consulter/poser le replay).
│   │   ├── webhooks.py           /webhook/digikuntz (callback statut entrant).
│   │   ├── auth.py               /signup,/login,/refresh,/logout : compte dev (self-service).
│   │   ├── projects.py           /apps,/apps/{id}/keys : le dev gère SES apps/clés (require_dev).
│   │   ├── admin.py              /admin/* : supervision developers/apps/clés + /admin/apps/{app_id}/pay
│   │   │                         + /admin/apps/{app_id}/payout + .../balance + /ledger/backfill
│   │   │                         + /digikuntz/balance (solde global) + /reconciliation (require_admin).
│   │   └── dev.py                /drive, /test-llm.
│   ├── schemas/                  Modèles Pydantic par domaine (payments, system, templates, dev,
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
│   ├── mock_aggregator.py        Mode mock (MOCK_PAYMENTS=true) : simule un paiement sans appeler
│   │                             DigiKUNTZ (ussd_sent immédiat puis successful/cancelled après délai).
│   │                             Socket.IO + webhook se déclenchent normalement. Utile si DigiKUNTZ down.
│   ├── config.py                 settings centralisés depuis .env (DigiKUNTZ, FLW, LLM, Supabase,
│   │                             retry_window_* par réseau, max_tabs_per_browser, mock_payments,
│   │                             digikuntz.use_callback).
│   ├── db.py                     Couche Supabase async (no-op si non configuré) : transactions,
│   │                             curl_templates, transaction_traces, transaction_errors, app_settings.
│   ├── db_ledger.py              LedgerMixin (hérité par Database) : comptabilité —
│   │                             app_ledger (credit_app/get_app_balance/reserve_payout/backfill/total),
│   │                             platform_ledger (credit_platform/debit_platform/get_platform_balance),
│   │                             aggregators (get/list/upsert_aggregator_config),
│   │                             payouts (insert_pending_payout/last_payout_for_account/update_payout).
│   ├── fees.py                   Calcul de ventilation des frais (compute_fees / fees_info) :
│   │                             brut → frais agrégateur → net → commission MW → app + plateforme.
│   ├── browser.py                BrowserSession (1 transaction = 1 contexte isolé : page, capture
│   │                             réseau, frame active, snapshot, actions, wait_for_page_change) +
│   │                             BrowserController (POOL : acquire/release_session, N sessions par
│   │                             Chrome puis 2e Chrome). CapturedRequest, DomSnapshot.
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
│       ├── aggregator.py         DigikuntzAggregator : implémente l'ABC, s'enregistre, replay (steps
│       │                         + ReplayConfig par appel), extract_curl_template, matchers.
│       ├── browser_flow.py       Mode navigateur : browser_objective (prompt FR — le navigateur
│       │                         S'ARRÊTE dès l'USSD demandé), decide_browser_outcome (pose
│       │                         poll_after_close, tab fermée), finalize_after_close (polling
│       │                         verify HORS navigateur), _interpret/_friendly.
│       ├── status_poll.py        Polling statut DigiKUNTZ (GET {base}/transaction?transactionId=) :
│       │                         source de vérité du verdict après USSD. payin_* -> interne, poll
│       │                         jusqu'à un verdict terminal (sans timeout : l'opérateur tranche toujours). 0 token.
│       ├── replay_flow.py        Mode curl replay : step1..step4 (create/init/charge/poll verify),
│       │                         ReplayConfig (config par appel, pas de globals mutés), interpret_*.
│       └── payout_flow.py        Payout (retrait) : initiate_payout (POST {base}/payout) +
│                                 poll_payout_status (GET {base}/transaction, mapping payout_*,
│                                 registre webhook⇄polling). Aucun navigateur/LLM/Flutterwave.
│
├── schema/
│   ├── supabase.sql              Schéma de base : transactions, curl_templates.
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
│
├── docs/openapi.json             Swagger versionné (régénérer via scripts/dump_openapi.py).
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
   - dernière `cancelled` (depuis `ussd_sent_at`) ou `successful` (depuis
     `validated_at`) dans le délai anti-doublon → 409 retry_too_soon ;
   - `failed`/panne → relançable tout de suite.
2. Insère la transaction `pending`, dispatch selon `mode` (auto/browser/replay) vers
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
  `Σ soldes apps + solde plateforme == solde global DigiKUNTZ` (cf. `/admin/reconciliation`).
- **Taux de commission par agrégateur** : stockés en BD (`aggregators`, migration 019),
  modifiables à chaud via `PUT /admin/aggregators/{name}/fees`. Deux taux :
  - `aggregator_fee_rate` : ce que prend l'agrégateur sur le brut (DigiKUNTZ = 5%) ;
  - `mw_commission_type/value` : commission MobileWallet sur le net (`percent` ou `flat`).
  Consultables publiquement via `GET /aggregators` et `GET /aggregators/{name}/fees`
  (avec simulation pour un montant donné).
- **Split comptable au payin** (`core/fees.py` → `compute_fees`) :
  brut → frais agrégateur → net → commission MW → `credit_app(net − comm)` +
  `credit_platform(frais + comm)`. Idempotent (même `transaction_id`), donc aucun
  double crédit même si polling et webhook concourent.
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
   verdict terminal (mapping `payout_*`, registre webhook⇄polling, sans timeout).
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

- Ajouter un agrégateur → `core/base.py` (l'ABC) + `aggregators/<nom>/`.
- Toucher la décision de l'IA → `core/reasoning_loop.py` + `aggregators/*/browser_flow.py`
  (objectif + decide_browser_outcome). **Ne jamais y remettre de "code qui décide".**
- Toucher la persistance → `core/db.py` (+ une migration dans `schema/migrations/`).
- Toucher les endpoints/modèles → `core/server.py` puis régénérer `docs/openapi.json`.
