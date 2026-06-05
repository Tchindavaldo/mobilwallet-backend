"""État runtime partagé entre les routers + helpers transverses.

`browser` et `llm` sont initialisés par le lifespan (core/server.py) et lus par
les handlers de plusieurs routers. Les exposer ici (un seul point) évite des
imports croisés avec server.py et garde chaque router focalisé sur ses routes.

Accès via les getters `get_browser()` / `get_llm()` : ils renvoient l'instance
vivante (le lifespan a réassigné les variables de module au démarrage).
"""

import logging
from datetime import datetime, timezone

from core.browser import BrowserController
from core.llm_client import LlmClient

log = logging.getLogger("ai_browser2")

# Réassignés par le lifespan au démarrage (core/server.py).
browser: BrowserController | None = None
llm: LlmClient | None = None


def get_browser() -> BrowserController | None:
    return browser


def get_llm() -> LlmClient | None:
    return llm


def seconds_since(created_at) -> float | None:
    """Secondes écoulées depuis un timestamp Supabase (ISO str), ou None si inconnu.

    Supabase renvoie created_at en ISO-8601 (UTC, souvent avec 'Z' ou +00:00).
    Retourne None sur toute erreur de parse pour que la garde dégrade en 'allow'
    plutôt que de bloquer sur une valeur malformée.
    """
    if not created_at:
        return None
    try:
        s = created_at.replace("Z", "+00:00") if isinstance(created_at, str) else created_at
        dt = datetime.fromisoformat(s) if isinstance(s, str) else s
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds()
    except (ValueError, TypeError) as e:
        log.warning("seconds_since parse failed for %r: %s", created_at, e)
        return None


def fmt_duration(seconds: int) -> str:
    """Durée FR lisible pour les messages de retry (ex. '12 min 30 s')."""
    seconds = max(0, int(seconds))
    m, s = divmod(seconds, 60)
    if m and s:
        return f"{m} min {s} s"
    if m:
        return f"{m} min"
    return f"{s} s"
