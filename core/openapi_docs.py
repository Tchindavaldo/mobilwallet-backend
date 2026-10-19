"""Documentation Swagger : schéma OpenAPI complet + une vue par agrégateur.

/docs montre d'abord les routes communes à tous les agrégateurs (chaque champ
précise à quels agrégateurs il s'applique), puis, en bas, un onglet par
agrégateur avec les routes qu'il est seul à avoir. L'authentification
(Authorize) est commune.

Tout est déduit des capacités déclarées par les agrégateurs (registre,
`direct_api`, modules `operations` et `availability`) :
aucun nom de prestataire ici.
"""

import copy
import importlib
import json

from fastapi import Depends, FastAPI, HTTPException
from fastapi.openapi.docs import get_redoc_html
from fastapi.openapi.utils import get_openapi
from fastapi.responses import HTMLResponse

from core import registry
from core.auth import require_admin

DESCRIPTION = """
Backend **MobileWallet** : une seule API pour encaisser (dépôt Mobile Money) et
virer (payout) via plusieurs **agrégateurs**.

Cette page liste les routes **communes** à tous les agrégateurs (champ
`aggregator` pour choisir le vôtre). Les routes **propres à un agrégateur**
sont **en bas de page**, dans un onglet par agrégateur.

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


def _direct(aggregator: str) -> bool:
    return bool(getattr(registry.get(aggregator), "direct_api", False))


def _owners(method: str, path: str) -> list[str]:
    """Agrégateurs pour lesquels MobileWallet propose cette route."""
    return [a for a in registry.names() if _keep(a, method, path, _direct(a))]


def _is_admin(op: dict) -> bool:
    """Route protégée par la clé admin (exigence de sécurité AdminKey)."""
    return any("AdminKey" in req for req in op.get("security") or [])


def _subset(app: FastAPI, title: str, keep, admin: bool = False) -> dict:
    """Copie du schéma complet réduite aux routes `keep(method, path)` : routes
    admin seulement si `admin`, sinon aucune route admin."""
    schema = copy.deepcopy(full_schema(app))
    schema["info"]["title"] = title
    paths = {}
    for path, ops in schema["paths"].items():
        kept = {m: o for m, o in ops.items() if keep(m, path) and _is_admin(o) == admin}
        if kept:
            paths[path] = kept
    schema["paths"] = paths
    used = {t for ops in paths.values() for o in ops.values() for t in o.get("tags", [])}
    schema["tags"] = [t for t in schema.get("tags", []) if t["name"] in used]
    return schema


def _annotate_fields(schema: dict) -> None:
    """Précise, champ par champ, à quels agrégateurs il s'applique."""
    names = registry.names()
    browser = [a for a in names if not _direct(a)]
    direct = [a for a in names if _direct(a)]
    for model in schema.get("components", {}).get("schemas", {}).values():
        props = model.get("properties") or {}
        if "aggregator" in props:
            props["aggregator"] = {**props["aggregator"], "enum": names}
        for f in _BROWSER_FIELDS:
            if f in props and browser:
                note = f" **Uniquement pour : {', '.join(browser)}**"
                if direct:
                    note += f" (ignoré par {', '.join(direct)})"
                props[f] = {**props[f],
                            "description": (props[f].get("description") or "") + note + "."}


def common_schema(app: FastAPI) -> dict:
    """Routes proposées pour TOUS les agrégateurs, champs annotés par agrégateur."""
    n = len(registry.names())
    schema = _subset(app, app.title, lambda m, p: len(_owners(m, p)) == n)
    _annotate_fields(schema)
    return schema


def aggregator_schema(app: FastAPI, aggregator: str) -> dict:
    """Routes propres à `aggregator` (proposées pour lui mais pas pour tous)."""
    if registry.get(aggregator) is None:
        raise HTTPException(404, {"error": "unknown_aggregator",
                                  "supported_aggregators": registry.names()})
    n = len(registry.names())

    def keep(m, p):
        owners = _owners(m, p)
        return aggregator in owners and len(owners) < n
    schema = _subset(app, f"Propre à {aggregator}", keep)
    _annotate_fields(schema)
    schema["info"]["description"] = ""
    return schema


def admin_schema(app: FastAPI) -> dict:
    """Routes admin seules (servies uniquement avec une AdminKey valide)."""
    schema = _subset(app, "Administration", lambda m, p: True, admin=True)
    schema["info"]["description"] = ""
    return schema


def install(app: FastAPI) -> None:
    """/docs : routes communes, onglets par agrégateur, et section admin chargée
    seulement après une AdminKey valide. Schémas publics sans aucune route admin :
    /openapi.json, /openapi/common.json, /openapi/<agrégateur>.json ;
    /openapi/admin.json exige X-Admin-Key. app.openapi() (dump versionné) reste
    complet. L'app doit être créée avec docs_url=None et openapi_url=None."""
    app.openapi = lambda: full_schema(app)

    @app.get("/openapi.json", include_in_schema=False)
    async def openapi_public():
        return _subset(app, app.title, lambda m, p: True)

    @app.get("/redoc", include_in_schema=False)
    async def redoc() -> HTMLResponse:
        return get_redoc_html(openapi_url="/openapi.json", title=app.title)

    @app.get("/openapi/admin.json", include_in_schema=False,
             dependencies=[Depends(require_admin)])
    async def openapi_admin():
        return admin_schema(app)

    @app.get("/openapi/common.json", include_in_schema=False)
    async def openapi_common():
        return common_schema(app)

    @app.get("/openapi/{aggregator}.json", include_in_schema=False)
    async def openapi_for(aggregator: str):
        return aggregator_schema(app, aggregator)

    @app.get("/docs", include_in_schema=False)
    async def docs() -> HTMLResponse:
        return HTMLResponse(_SWAGGER_HTML.replace("__TITLE__", app.title)
                            .replace("__AGGS__", json.dumps(registry.names())))


# Deux Swagger UI : le haut (routes communes) et le bas (onglet de l'agrégateur
# choisi). Leurs autorisations sont synchronisées (plugin syncAuth) et
# conservées (persistAuthorization) : on ne remplit Authorize qu'une fois.
_CDN = "https://cdn.jsdelivr.net/npm/swagger-ui-dist@5.17.14"
_SWAGGER_HTML = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>__TITLE__</title>
<link rel="icon" href="https://fastapi.tiangolo.com/img/favicon.png">
<link rel="stylesheet" href="{_CDN}/swagger-ui.css">
<style>
 body {{ margin: 0; font-family: sans-serif; }}
 .block {{ max-width: 1460px; margin: 40px auto 80px; padding: 0 20px; }}
 .block h2 {{ font-size: 28px; color: #3b4151; margin: 0 0 6px; }}
 .block p {{ color: #3b4151; margin: 0 0 16px; }}
 .tabs {{ display: flex; gap: 8px; border-bottom: 2px solid #e3e3e3; }}
 .tabs button {{ border: 0; background: none; padding: 10px 18px; font-size: 16px;
   font-weight: 600; color: #6b6b6b; cursor: pointer; border-bottom: 3px solid transparent;
   margin-bottom: -2px; }}
 .tabs button.active {{ color: #49cc90; border-bottom-color: #49cc90; }}
 #swagger-specific .information-container, #swagger-specific .scheme-container,
 #swagger-admin .information-container, #swagger-admin .scheme-container {{ display: none; }}
</style>
</head><body>
<div id="swagger-common"></div>
<section id="admin-section" class="block" hidden>
  <h2>Administration</h2>
  <p>Visible parce qu'une AdminKey valide est saisie dans Authorize.</p>
  <div id="swagger-admin"></div>
</section>
<section id="specific" class="block">
  <h2>Endpoints propres à chaque agrégateur</h2>
  <p>Routes que MobileWallet ne propose que pour l'agrégateur choisi. La connexion (Authorize) reste la même.</p>
  <div class="tabs" id="tabs"></div>
  <div id="swagger-specific"></div>
</section>
<script src="{_CDN}/swagger-ui-bundle.js"></script>
<script>
const AGGS = __AGGS__;
const uis = {{}};
let syncing = false;
function syncAuth(self) {{
  const relay = (name) => (ori) => (payload) => {{
    const res = ori(payload);
    if (!syncing) {{
      syncing = true;
      for (const [k, ui] of Object.entries(uis)) if (k !== self && ui) ui.authActions[name](payload);
      syncing = false;
      onAuth(name, payload);
    }}
    return res;
  }};
  return () => ({{ statePlugins: {{ auth: {{ wrapActions: {{
    authorize: relay("authorize"), logout: relay("logout") }} }} }} }});
}}
function make(key, domId, url) {{
  return SwaggerUIBundle({{ url, dom_id: domId, presets: [SwaggerUIBundle.presets.apis],
    plugins: [syncAuth(key)], persistAuthorization: true,
    // Liens directs (#/tag/route) : seule l'instance du haut pilote l'URL.
    deepLinking: key === "common", showExtensions: true, showCommonExtensions: true,
    defaultModelsExpandDepth: -1 }});
}}
// Section admin : le serveur ne livre ses routes qu'avec une AdminKey valide.
async function loadAdmin(key) {{
  const sec = document.getElementById("admin-section");
  if (!key) {{ sec.hidden = true; uis.admin = null; return; }}
  const r = await fetch("/openapi/admin.json", {{ headers: {{ "X-Admin-Key": key }} }});
  if (!r.ok) {{ sec.hidden = true; uis.admin = null; return; }}
  sec.hidden = false;
  uis.admin = SwaggerUIBundle({{ spec: await r.json(), dom_id: "#swagger-admin",
    presets: [SwaggerUIBundle.presets.apis], plugins: [syncAuth("admin")],
    persistAuthorization: true, deepLinking: false, defaultModelsExpandDepth: -1 }});
}}
function onAuth(name, payload) {{
  if (name === "authorize" && payload && payload.AdminKey) loadAdmin(payload.AdminKey.value);
  if (name === "logout" && [].concat(payload || []).includes("AdminKey")) loadAdmin(null);
}}
uis.common = make("common", "#swagger-common", "/openapi/common.json");
try {{
  const saved = JSON.parse(localStorage.getItem("authorized") || "{{}}");
  if (saved.AdminKey) loadAdmin(saved.AdminKey.value);
}} catch (e) {{}}
function show(agg) {{
  document.querySelectorAll("#tabs button").forEach(b => b.classList.toggle("active", b.dataset.agg === agg));
  uis.specific = make("specific", "#swagger-specific", "/openapi/" + agg + ".json");
}}
for (const a of AGGS) {{
  const b = document.createElement("button");
  b.textContent = a; b.dataset.agg = a; b.onclick = () => show(a);
  document.getElementById("tabs").appendChild(b);
}}
show(AGGS[0]);
</script></body></html>"""
