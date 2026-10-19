"""Documentation Swagger : schéma OpenAPI complet + une vue par agrégateur.

/docs propose un sélecteur (barre du haut) : « Tous » ou un agrégateur. Chaque
vue ne garde que les routes que MobileWallet propose pour cet agrégateur, sans
les champs qui ne le concernent pas. L'authentification
(Authorize) est commune à toutes les vues (persistAuthorization).

Tout est déduit des capacités déclarées par les agrégateurs (registre,
`direct_api`, modules `operations` et `availability`) :
aucun nom de prestataire ici.
"""

import copy
import importlib
import json

from fastapi import FastAPI, HTTPException
from fastapi.openapi.utils import get_openapi
from fastapi.responses import HTMLResponse

from core import registry

DESCRIPTION = """
Backend **MobileWallet** : une seule API pour encaisser (dépôt Mobile Money) et
virer (payout) via plusieurs **agrégateurs**.

**Sélecteur en haut de page** : choisissez votre agrégateur pour ne voir que
les routes qui le concernent. La connexion (**Authorize**) est commune.

### Démarrage rapide
1. **Compte** : `POST /signup` (ou `POST /login`) → copiez `access_token`, puis
   **Authorize → DevJWT**.
2. **App + clé** : `POST /apps` (notez `id`) puis `POST /apps/{app_id}/keys`
   → copiez la clé `sk_…` (affichée **une seule fois**), puis **Authorize → ApiKey**.
3. **Payer** : `GET /availability` (état des opérateurs) puis `POST /pay`, et
   suivez avec `GET /status/{transaction_ref}`.

Dans **Authorize**, collez la valeur seule (sans « Bearer »).
**AdminKey** : réservée à l'administrateur MobileWallet (routes `/admin`).
"""

TAGS = [
    {"name": "auth", "description": "Compte developer : inscription, connexion, jetons (DevJWT)."},
    {"name": "projects", "description": "Vos apps et leurs clés API (DevJWT)."},
    {"name": "availability", "description": "Opérateurs disponibles, montants min/max, prédiction du réseau."},
    {"name": "payments", "description": "Encaisser : dépôt Mobile Money, page de paiement hébergée."},
    {"name": "payout", "description": "Virer : retraits débités du solde de l'app."},
    {"name": "transactions", "description": "Suivi : statut, historique, solde."},
    {"name": "system", "description": "Santé du service et agrégateurs disponibles."},
    {"name": "admin", "description": "Réservé à l'administrateur MobileWallet (AdminKey)."},
    {"name": "dev", "description": "Outils internes (navigateur IA)."},
]

SECURITY_SCHEMES = {
    "ApiKey": {"type": "http", "scheme": "bearer",
               "description": "Clé API de votre app (`sk_…`), obtenue par "
                              "`POST /apps/{app_id}/keys`. Paiements, virements, suivi."},
    "DevJWT": {"type": "http", "scheme": "bearer",
               "description": "`access_token` renvoyé par `POST /signup` ou `POST /login`. "
                              "Gestion de vos apps et clés."},
    "AdminKey": {"type": "apiKey", "in": "header", "name": "X-Admin-Key",
                 "description": "Clé admin MobileWallet (secret serveur ADMIN_KEY). Routes /admin."},
}

# Route -> opération optionnelle (aggregators.<nom>.operations.<op>) qu'elle exige.
_ROUTE_OPS = {
    ("post", "/pay/page"): "create_payment_page",
    ("post", "/operators/predict"): "predict_network",
    ("post", "/payout/bulk"): "initiate_payouts_bulk",
    ("post", "/payout/{transaction_ref}/cancel"): "cancel_payout",
    ("post", "/status/{transaction_ref}/resend-callback"): "resend_callback",
}
# Routes du navigateur IA : absentes pour un agrégateur à API directe.
_BROWSER_PATHS = ("/transactions/{transaction_ref}/trace", "/transactions/{transaction_ref}/errors",
                  "/config/max-tabs", "/drive", "/test-llm")
_BROWSER_FIELDS = ("mode", "fallback_browser")
_NETWORK_ROUTE = "/availability"


def full_schema(app: FastAPI) -> dict:
    if app.openapi_schema:
        return app.openapi_schema
    schema = get_openapi(title=app.title, version=app.version, description=DESCRIPTION,
                         tags=TAGS, routes=app.routes)
    schema.setdefault("components", {})["securitySchemes"] = SECURITY_SCHEMES
    app.openapi_schema = schema
    return schema


def _has_module(aggregator: str, module: str, attr: str | None = None) -> bool:
    try:
        mod = importlib.import_module(f"aggregators.{aggregator}.{module}")
    except ModuleNotFoundError:
        return False
    return attr is None or hasattr(mod, attr)


def _keep(aggregator: str, method: str, path: str, direct_api: bool) -> bool:
    op = _ROUTE_OPS.get((method, path))
    if op:
        return _has_module(aggregator, "operations", op)
    if path == _NETWORK_ROUTE:
        return _has_module(aggregator, "availability")
    if direct_api and path in _BROWSER_PATHS:
        return False
    # Routes admin propres à un AUTRE agrégateur (/admin/<autre>/...).
    parts = path.split("/")
    if len(parts) > 2 and parts[1] == "admin" and parts[2] in registry.names():
        return parts[2] == aggregator
    return True


def _fix_schemas(schema: dict, aggregator: str, direct_api: bool) -> None:
    for model in schema.get("components", {}).get("schemas", {}).values():
        props = model.get("properties") or {}
        if "aggregator" in props:
            props["aggregator"] = {**props["aggregator"], "default": aggregator,
                                   "enum": [aggregator]}
        if direct_api:
            for f in _BROWSER_FIELDS:
                props.pop(f, None)
                if f in (model.get("required") or []):
                    model["required"].remove(f)
    # Paramètres de requête `aggregator` (ex. /availability?aggregator=).
    for ops in schema["paths"].values():
        for op in ops.values():
            for p in op.get("parameters", []):
                if p.get("name") == "aggregator":
                    p["schema"] = {**p.get("schema", {}), "default": aggregator, "enum": [aggregator]}


def aggregator_schema(app: FastAPI, aggregator: str) -> dict:
    cls = registry.get(aggregator)
    if cls is None:
        raise HTTPException(404, {"error": "unknown_aggregator",
                                  "supported_aggregators": registry.names()})
    direct_api = bool(getattr(cls, "direct_api", False))
    schema = copy.deepcopy(full_schema(app))
    schema["info"]["title"] = f"MobileWallet backend · {aggregator}"
    paths = {}
    for path, ops in schema["paths"].items():
        kept = {m: o for m, o in ops.items() if _keep(aggregator, m, path, direct_api)}
        if kept:
            paths[path] = kept
    schema["paths"] = paths
    _fix_schemas(schema, aggregator, direct_api)
    used = {t for ops in paths.values() for o in ops.values() for t in o.get("tags", [])}
    schema["tags"] = [t for t in schema.get("tags", []) if t["name"] in used]
    return schema


def install(app: FastAPI) -> None:
    """Remplace /docs par la version à sélecteur et ajoute /openapi/<agrégateur>.json.
    L'app doit être créée avec docs_url=None."""
    app.openapi = lambda: full_schema(app)

    @app.get("/openapi/{aggregator}.json", include_in_schema=False)
    async def openapi_for(aggregator: str):
        return aggregator_schema(app, aggregator)

    @app.get("/docs", include_in_schema=False)
    async def docs() -> HTMLResponse:
        urls = [{"name": n, "url": f"/openapi/{n}.json"} for n in registry.names()]
        urls.append({"name": "Tous les agrégateurs", "url": app.openapi_url})
        return HTMLResponse(_SWAGGER_HTML.replace("__TITLE__", app.title)
                            .replace("__URLS__", json.dumps(urls)))


# Swagger UI avec la barre du haut (StandaloneLayout) : c'est elle qui porte le
# sélecteur « Select a definition ». La page FastAPI par défaut ne la charge pas.
_CDN = "https://cdn.jsdelivr.net/npm/swagger-ui-dist@5.17.14"
_SWAGGER_HTML = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>__TITLE__</title>
<link rel="stylesheet" href="{_CDN}/swagger-ui.css">
<style>.topbar-wrapper .link {{ visibility: hidden; }}</style>
</head><body><div id="swagger-ui"></div>
<script src="{_CDN}/swagger-ui-bundle.js"></script>
<script src="{_CDN}/swagger-ui-standalone-preset.js"></script>
<script>
window.ui = SwaggerUIBundle({{
  urls: __URLS__,
  dom_id: "#swagger-ui",
  presets: [SwaggerUIBundle.presets.apis, SwaggerUIStandalonePreset],
  layout: "StandaloneLayout",
  persistAuthorization: true,
  deepLinking: true,
}});
</script></body></html>"""
