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
from core import runtime
from core.browser import BrowserController
from core.config import settings
from core.db import db
from core.llm_client import LlmClient, LlmConfig
from core.routers import dev, payments, system, templates, transactions, webhooks

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

    log.info("AI Browser 2 ready! Browser=%s, Model=%s",
             "headless" if headless else "visible",
             runtime.llm.config.model)
    yield

    # Cleanup
    await runtime.llm.close()
    await runtime.browser.stop()


API_DESCRIPTION = """
Backend **MobileWallet** — un rassemblement d'**agrégateurs** de paiement Mobile Money.

Chaque agrégateur (DigiKUNTZ aujourd'hui, d'autres demain) est un module exposant
deux capacités :

- **navigateur IA** (`mode=browser`) : un agent Playwright + LLM pilote le checkout,
  exécute le paiement et **déduit** le « curl replay » (requête `/charge` + `/verify`),
  persisté comme *template* réutilisable.
- **curl replay** (`mode=replay`) : rejoue le paiement sans navigateur via le template stocké.
- **auto** (`mode=auto`, défaut) : tente le replay d'abord, puis bascule sur le navigateur
  si le replay est non concluant (`fallback_browser`).

Toute tentative est auditée (table `transactions`) quand Supabase est configuré.
"""

OPENAPI_TAGS = [
    {"name": "system", "description": "Santé et introspection du service."},
    {"name": "payments", "description": "Exécution des paiements via les agrégateurs."},
    {"name": "transactions", "description": "Gestion des transactions exposée au client."},
    {"name": "admin", "description": "Audit/debug réservé à l'admin backend (non documenté pour le client)."},
    {"name": "dev", "description": "Outils de développement (pilotage libre, ping LLM)."},
]

app = FastAPI(
    title="MobileWallet backend",
    version="0.2",
    description=API_DESCRIPTION,
    openapi_tags=OPENAPI_TAGS,
    lifespan=lifespan,
)

# Montage des routers par domaine.
app.include_router(system.router)
app.include_router(webhooks.router)
app.include_router(payments.router)
app.include_router(transactions.router)
app.include_router(templates.router)
app.include_router(dev.router)
