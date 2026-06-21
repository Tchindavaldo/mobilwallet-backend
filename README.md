# MobileWallet — backend

Backend d'agrégation de paiements **Mobile Money** (XAF). MobileWallet rassemble des
**agrégateurs** de paiement ; chacun est un module exposant deux capacités :

- **navigateur IA** — un agent Playwright + LLM pilote le checkout, exécute le paiement
  et **déduit** la requête `/charge` + `/verify` (le « curl replay »).
- **curl replay** — rejoue le paiement sans navigateur via un *template* stocké.

DigiKUNTZ est le premier agrégateur. Le navigateur IA est **générique** (mutualisé) ;
le curl replay est **propre à chaque agrégateur**.

> Pour une **vision 360** (carte de tous les fichiers, rôle de chaque module, flux
> d'un paiement, statuts, concurrence), voir **[`ARCHITECTURE.md`](ARCHITECTURE.md)**.

---

## Architecture

```
ai_browser2/
├── main.py                     # lanceur : importe core.server:app, lance uvicorn
├── core/                       # MOTEUR GÉNÉRIQUE (partagé)
│   ├── server.py               #   assemblage FastAPI + montage Socket.IO (export `asgi`)
│   ├── runtime.py              #   état partagé (browser/llm) + helpers transverses
│   ├── routers/                #   un APIRouter par domaine (system, payments, admin, …)
│   ├── schemas/                #   modèles Pydantic par domaine
│   ├── auth.py                 #   auth des clés API d'app (paiements) + require_admin
│   ├── dev_auth.py             #   auth du compte dev (self-service) : bcrypt + JWT + require_dev
│   ├── tenants.py              #   persistance multi-tenant + comptes/refresh + isolation
│   ├── notifications.py        #   verdict -> client : webhook signé + push Socket.IO
│   ├── realtime.py             #   serveur Socket.IO (rooms par app, auth par clé)
│   ├── base.py                 #   interface Aggregator (ABC) + dataclasses
│   ├── registry.py             #   registre nom -> classe d'agrégateur
│   ├── config.py               #   settings centralisés (.env)
│   ├── db.py                   #   couche Supabase (async, optionnelle)
│   ├── browser.py              #   navigateur IA (Playwright, capture, matchers)
│   ├── browser_runner.py       #   orchestration navigateur générique
│   ├── reasoning_loop.py       #   boucle IA (snapshot -> LLM -> actions)
│   ├── llm_client.py           #   client DeepSeek
│   ├── classifier.py           #   classification de statut (mots-clés)
│   └── crypto/                 #   encrypt.js + cryptico_py.py
├── aggregators/                # UN DOSSIER PAR AGRÉGATEUR
│   └── digikuntz/
│       ├── aggregator.py       #   DigikuntzAggregator (implémente l'ABC, s'enregistre)
│       ├── browser_flow.py     #   mode navigateur (objectif IA, watch USSD, verdict)
│       └── replay_flow.py      #   mode curl replay (steps + interpret)
├── schema/supabase.sql         # tables transactions + curl_templates
├── .env.example                # clés de configuration
└── requirements.txt
```

**3 couches** : `server.py` + `routers/` (API/routing, agnostique) → `base.py` (contrat
`Aggregator`) → `aggregators/<nom>/` (logique métier). La persistance (`db.py`) est appelée
par la couche API.

---

## Installation & lancement

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
playwright install chromium          # navigateur pour le mode browser
cp .env.example .env                 # puis remplir les valeurs

./start.sh                           # démarre l'API (+ navigateur au boot)
./dev.sh                             # dev : hot-reload sur changement de .py
```

> Les scripts utilisent le venv automatiquement. Équivalents longs :
> `venv/bin/python main.py` et `RELOAD=1 venv/bin/python main.py`.

> Le hot-reload (`RELOAD=1`) surveille le **code** (`.py`), pas le `.env`. Après avoir
> modifié `.env` (ex: ajout Supabase), il faut **couper et relancer** le serveur.

Variables d'environnement (cf. `.env.example`) : `DIGIKUNTZ_*`, `FLW_*`, `DEEPSEEK_API_KEY`,
`SUPABASE_URL`/`SUPABASE_KEY`, `HEADLESS` (1 en prod), `PORT`, `HOST`,
`DIGIKUNTZ_USE_CALLBACK` (transmettre un `callbackUrl` à DigiKUNTZ, défaut `false`),
`MOCK_PAYMENTS` (mode mock, cf. plus bas).

> Sans Supabase configuré, l'API fonctionne quand même : la persistance devient un no-op
> (pas d'historique ni de template stocké).

---

## Documentation interactive (Swagger)

FastAPI génère automatiquement la doc OpenAPI :

- **Swagger UI** : `http://localhost:7332/docs`
- **ReDoc** : `http://localhost:7332/redoc`
- **Schéma brut** : `http://localhost:7332/openapi.json`

---

## Endpoints

**Endpoints CLIENT** (documentés dans Swagger) :

| Méthode | Route | Rôle |
|---|---|---|
| GET | `/health` | statut + agrégateurs |
| GET | `/aggregators` | modules disponibles + réseaux + taux de frais |
| GET | `/aggregators/{name}/fees` | taux détaillés + simulation pour un montant |
| POST | `/pay` | exécuter un paiement (encaissement) |
| POST | `/payout` | effectuer un virement (retrait), débité du solde de l'app |
| GET | `/balance` | solde courant de votre app (XAF) |
| POST | `/transactions/{tx_id}/cancel` | débloquer une transaction `pending` |
| GET/PUT | `/config/max-tabs` | seuil d'onglets par navigateur (concurrence) |

**Endpoints ADMIN/DEBUG** (masqués de Swagger ; outils d'audit pour le backend) :
`GET /transactions`, `GET /status/{ref}`, `GET /transactions/{ref}/trace`,
`GET /transactions/{ref}/errors`, `GET|POST /aggregators/{name}/template`,
`POST /drive`, `POST /test-llm`. *(À terme : endpoint admin protégé par auth — cf.
`todo/migrer-vue-admin-endpoint-dedie.md`.)*

**Endpoints ADMIN comptabilité** (header `X-Admin-Key`) :

| Méthode | Route | Rôle |
|---|---|---|
| POST | `/admin/apps/{id}/payout` | virement pour une app (débité de SON solde) |
| GET | `/admin/apps/{id}/balance` | solde d'une app |
| POST | `/admin/ledger/backfill` | amorcer les soldes depuis les payins réussis |
| GET | `/admin/digikuntz/balance` | solde réel du compte global DigiKUNTZ |
| GET | `/admin/reconciliation` | Σ soldes apps + plateforme vs solde global |
| GET | `/admin/platform/balance` | solde plateforme (argent propre MobileWallet) |
| POST | `/admin/platform/credit` | recharger le solde plateforme |
| POST | `/admin/platform/payout` | virement débité de la plateforme (sans contrôle de solde) |
| GET | `/admin/aggregators/{name}/fees` | lire la config de commission d'un agrégateur |
| PUT | `/admin/aggregators/{name}/fees` | modifier les taux (effet immédiat, sans redémarrage) |

> **Solde plateforme** : l'argent propre de MobileWallet (marge/frais/flottant),
> distinct des apps clientes. L'admin le recharge (`/admin/platform/credit`) et
> retire dessus (`/admin/platform/payout`) **sans contrôle de solde** (le solde peut
> devenir négatif puis être rechargé). Inclus dans l'invariant et la réconciliation.

> **Vue client vs admin** : `/pay` renvoie par défaut une **vue minimale**
> (`success`, `status`, `message`, `transaction_id`, `code`). L'admin peut ajouter
> `?debug=true` (non documenté dans Swagger) pour obtenir tout le détail technique
> (charge/verify, curl déduit, tokens, trace…).

### `POST /pay`

```jsonc
{
  "amount": 25,                 // XAF
  "phone": "696080087",
  "network": "Orangemoney",     // ou "MTN"
  "email": "client@example.com",
  "aggregator": "digikuntz",
  "mode": "auto",               // "auto" | "browser" | "replay"
  "fallback_browser": true      // mode auto : bascule navigateur si replay non concluant
}
```

**Modes :**
- `auto` (défaut) — tente le **replay** ; si non concluant (pas de template / `failed` /
  `unknown`), bascule sur le **navigateur** quand `fallback_browser=true`.
- `browser` — flux IA complet ; **déduit et persiste** le template curl.
- `replay` — rejoue via le template stocké. **409** si aucun template (lancer `browser` d'abord).

> **Mode mock** (`MOCK_PAYMENTS=true`) — `/pay` simule un paiement sans appeler DigiKUNTZ :
> réponse `ussd_sent` immédiate, puis verdict final (`successful`/`cancelled`) après un court
> délai, poussé normalement via **Socket.IO + webhook**. Utile pour développer/tester quand
> DigiKUNTZ est indisponible. Implémenté dans `core/mock_aggregator.py`.

**Codes d'erreur :** `404` agrégateur inconnu · `400` mode invalide · `422` réseau non
supporté (renvoie la liste exacte attendue) · `409` replay sans template · `502` replay
échoué et fallback désactivé.

**Réseaux supportés** : propres à chaque agrégateur. `GET /aggregators` les expose :

```json
{"aggregators":[{"name":"digikuntz","supported_networks":["Orangemoney","MTN"]}]}
```

Un `network` invalide renvoie `422` avec la liste exacte ; les variantes tolérées
(`orange` → `Orangemoney`) sont normalisées automatiquement.

Exemple :

```bash
curl -X POST localhost:7332/pay -H 'content-type: application/json' -d '{
  "amount":25,"phone":"696080087","network":"Orangemoney",
  "email":"client@example.com","mode":"browser"
}'
```

### `POST /payout` — virement sortant (retrait)

**MobileWallet est lui-même un agrégateur** : côté DigiKUNTZ il n'existe qu'**un
seul compte global** où atterrit l'argent de toutes les apps. C'est donc nous qui
tenons le **solde de chaque app** (grand livre `app_ledger`) :

> **solde d'une app** = somme de ses encaissements (`/pay`) réussis − ses retraits.

Un retrait **débite le solde de VOTRE app** (identifiée par la clé API). Invariant :
`Σ soldes des apps + solde plateforme == solde réel du compte global DigiKUNTZ` — une
app ne peut jamais retirer plus qu'elle n'a encaissé. Un virement supérieur au solde est
**refusé (422)** avant tout appel amont. Le solde est réservé (débité) à
l'initiation et **remboursé** si le virement échoue.

```jsonc
{
  "amount": 5000,
  "account_bank_code": "MTN",          // réseau / banque du bénéficiaire
  "account_number": "237691224472",
  "receiver_name": "John Doe",
  "currency": "XAF",
  "narration": "Paiement fournisseur",
  "aggregator": "digikuntz"
}
```

Réponse immédiate `status: "pending"`, puis verdict final (`successful` / `failed`
/ `cancelled`) poussé via **webhook + Socket.IO** et consultable par
`GET /status/{transaction_id}`. `GET /balance` renvoie le solde courant.

**Codes d'erreur :** `404` agrégateur inconnu · `400` agrégateur sans payout ·
`409` un virement déjà en cours vers ce bénéficiaire · `422` solde insuffisant ·
`502` virement non initié · `503` service amont indisponible.

> **Mode mock** (`MOCK_PAYMENTS=true`) — `/payout` simule le virement sans appeler
> DigiKUNTZ mais **applique la vraie comptabilité** (réserve/rembourse le solde),
> verdict après un court délai via Socket.IO + webhook.

```bash
curl -X POST localhost:7332/payout \
  -H 'authorization: Bearer sk_live_…' -H 'content-type: application/json' -d '{
  "amount":5000,"account_bank_code":"MTN","account_number":"237691224472",
  "receiver_name":"John Doe","currency":"XAF","narration":"Paiement fournisseur"
}'
```

---

## Persistance (Supabase)

Créer les tables via `schema/supabase.sql` puis renseigner `SUPABASE_URL`/`SUPABASE_KEY`.

- `transactions` — audit d'une tentative. Insérée en **`pending`** dès le départ, puis
  **mise à jour** (`status`/`success`/`message`) au verdict final. Un paiement ne peut pas
  être lancé sur un numéro qui a déjà une transaction `pending` (→ **409 `pending_exists`**).
- `curl_templates` — templates de replay **versionnés par agrégateur** (jamais mélangés).
  Le mode browser **ajoute** une nouvelle version **uniquement si la recette a changé**
  (sinon rien) ; la précédente est désactivée mais **conservée en historique**. Exactement
  une ligne `is_active=true` par agrégateur — c'est celle que le mode replay recharge.
  Chaque template porte un `status` (`untested`/`working`/`failed`, migration `014`) : le replay
  réutilise le dernier `working`, à défaut le dernier `untested`, et **jamais** un `failed`.

Les appels Supabase (synchrones) sont exécutés via `asyncio.to_thread` pour ne pas bloquer
la boucle asynchrone (ni le navigateur unique).

### Template manuel

Amorcer/corriger le template de replay sans passer par le mode browser :

```bash
# Lire le template actif
curl localhost:7332/aggregators/digikuntz/template

# Ajouter/mettre à jour (append-si-différent ; force=true pour forcer une version)
curl -X POST localhost:7332/aggregators/digikuntz/template \
  -H 'content-type: application/json' -d '{
    "charge_url": "https://api.ravepay.co/flwv3-pug/getpaidx/api/charge?use_polling=1",
    "verify_url": "https://api.ravepay.co/flwv3-pug/getpaidx/api/verify/mpesa",
    "public_key_rsa": "<clé RSA cryptico>",
    "flw_pub_key": "FLWPUBK-...-X",
    "headers": {"content-type": "application/json", "x-flw-lang": "FR"},
    "force": false
  }'
```

---

## Ajouter un agrégateur

1. Créer `aggregators/<nom>/`.
2. Implémenter une classe héritant de `core.base.Aggregator` (les méthodes abstraites :
   `create_transaction`, `browser_objective`, `decide_browser_outcome`, `network_label`,
   `charge_request_matcher`, `verify_request_matcher`, `checkout_url_predicate`,
   `extract_curl_template`, `interpret_status`, `replay`, `pay_via_browser`).
3. S'enregistrer à l'import : `register("<nom>", MonAggregator)`.
4. L'importer depuis `aggregators/<nom>/__init__.py`.

Aucune modification de `core/` n'est nécessaire — le navigateur IA générique et le routing
fonctionnent par contrat.

---

## Tests manuels

`POST /pay` déclenche une **vraie transaction** (et un prompt USSD `#150*50#` sur le
téléphone). Utiliser un petit montant et un numéro contrôlé.

> **Tester avec `./start.sh`** (sans hot-reload). Le `./dev.sh` recharge sur tout
> changement de `.py` et **coupe les requêtes `/pay` longues** (l'IA attend la
> validation USSD pendant toute la fenêtre opérateur) — Postman resterait en attente sans réponse.

**Comportement clé des deux moteurs (aligné)** :
- **Navigateur** : il s'**arrête dès que l'USSD est demandé** au client (statut
  `ussd_sent`) — il ne surveille pas la validation à l'écran (coûteux). Le statut
  final vient du **polling statut DigiKUNTZ** (`GET {base}/transaction?transactionId=`),
  jusqu'au verdict terminal (sans timeout : l'opérateur finit toujours par trancher ; un échec après USSD → `cancelled`).
  *(Webhook serveur possible une fois le backend déployé — cf. `todo/webhook-digikuntz.md`.)*
- Statuts finaux : `successful`, `failed` (refus / solde insuffisant),
  `cancelled` (refus USSD ou non-validation, par l'utilisateur ou l'opérateur),
  `pending`. Pannes amont → **503** `network_unavailable` (API) ou
  `operator_unavailable` (réseau opérateur dérangé).
- Garde anti-doublon : seuls `pending` et `cancelled` bloquent un nouveau paiement
  sur le même numéro ; `failed`/pannes sont **immédiatement relançables**.
- Replay : retry ×3 sur le charge, polling `/verify` jusqu'au verdict terminal (sans timeout).
