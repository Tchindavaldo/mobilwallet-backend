"""MobileWallet backend — assemblage FastAPI.

Ce fichier ne contient PAS de logique métier : il crée l'app, gère le cycle de
vie (pool navigateur + client LLM, stockés dans core/runtime) et monte les
routers par domaine (core/routers/). Chaque route vit dans son router dédié.

Un paiement est dispatché par (agrégateur, mode) :
  - mode="browser" : flux IA Playwright complet ; le template curl déduit est persisté.
  - mode="replay"  : rejoue le paiement via le template stocké, sans navigateur.
  - mode="auto"    : replay d'abord, bascule navigateur si non concluant.
Chaque tentative est auditée (table transactions) si Supabase est configuré.
"""

import logging
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI

# Importer le package aggregators enregistre tous les agrégateurs dans le registre.
import aggregators.digikuntz  # noqa: F401
import aggregators.pawapay  # noqa: F401
from core import openapi_docs, runtime
from core.browser import BrowserController
from core.config import settings
from core.db import db
from core.llm_client import LlmClient, LlmConfig
from core.routers import (
    admin, auth, availability, dev, payment_page, payments, payout, payout_ops, projects,
    system, templates, transactions,
    webhooks,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    stream=sys.stdout,
)
# Quiet the noisy third-party loggers so the AI workflow stands out in the
# console. Each HTTP call to DeepSeek/Supabase and every access line used to
# drown the agent's thoughts/actions.
for noisy in ("httpx", "httpcore", "uvicorn.access", "hpack", "openai"):
    logging.getLogger(noisy).setLevel(logging.WARNING)
log = logging.getLogger("ai_browser2")


async def _resume_unsettled() -> None:
    """Après un redémarrage (redéploiement), les suivis en mémoire sont perdus.
    On reprend chaque transaction non finale : une vérification chez
    l'agrégateur tout de suite (rattrape un verdict arrivé pendant la coupure),
    et pour une page de paiement une autre à son expiration (aucun webhook
    n'arrive si le client l'abandonne). Le reste arrive par webhook."""
    from datetime import datetime, timezone
    from core import aggregator_ops, status_check
    rows = await db.list_unsettled()
    for i, tx in enumerate(rows):
        ref = tx.get("transaction_ref")
        status_check.schedule_check(ref, 5 + i * 0.5)  # étalé : pas de rafale
        if tx.get("mode") != "page":
            continue
        try:
            expiry = aggregator_ops.get(tx["aggregator"], "PAGE_EXPIRY_S")
            created = datetime.fromisoformat(tx["created_at"].replace("Z", "+00:00"))
            remaining = expiry + 60 - (datetime.now(timezone.utc) - created).total_seconds()
            if remaining > 5 + i * 0.5:  # déjà expirée : la vérif immédiate suffit
                status_check.schedule_check(ref, remaining)
        except Exception as e:  # noqa: BLE001
            log.warning("reprise vérif page %s : %s", ref, e)
    if rows:
        log.info("Reprise de %d transaction(s) non finale(s) après démarrage", len(rows))


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Start browser pool (visible by default so you can watch!). The max-tabs
    # threshold comes from env, then is overridden by the DB value if present.
    headless = settings.headless
    max_tabs = await db.get_max_tabs(default=settings.max_tabs_per_browser)
    runtime.browser = BrowserController(headless=headless, max_tabs=max_tabs)
    await runtime.browser.start()

    # Init LLM client
    runtime.llm = LlmClient(LlmConfig(
        provider="deepseek",
        model=settings.llm_model,
        api_key=settings.deepseek_api_key,
    ))

    await _resume_unsettled()

    log.info("AI Browser 2 ready! Browser=%s, Model=%s",
             "headless" if headless else "visible",
             runtime.llm.config.model)
    yield

    # Cleanup
    await runtime.llm.close()
    await runtime.browser.stop()


app = FastAPI(
    title="MobileWallet backend",
    version="0.2",
    lifespan=lifespan,
    docs_url=None,
    openapi_url=None,  # servi par core/openapi_docs (sans routes admin)
)

# Montage des routers par domaine.
app.include_router(system.router)
app.include_router(webhooks.router)
app.include_router(auth.router)
app.include_router(projects.router)
app.include_router(payments.router)
app.include_router(payout_ops.router)
app.include_router(payout.router)
app.include_router(payment_page.router)
app.include_router(transactions.router)
app.include_router(availability.router)
app.include_router(templates.router)
app.include_router(admin.router)
app.include_router(dev.router)


# Swagger : schéma complet + une vue par agrégateur (sélecteur dans /docs).
openapi_docs.install(app)

# Monte Socket.IO par-dessus FastAPI. `asgi` est le point d'entrée à servir
# (uvicorn core.server:asgi) ; `app` reste l'app FastAPI pure (routes/openapi).
from core import realtime  # noqa: E402 — après la création de `app`

asgi = realtime.mount(app)
