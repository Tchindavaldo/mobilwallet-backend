"""Routes templates curl : consulter / poser manuellement le template de replay."""

import dataclasses

from fastapi import APIRouter, HTTPException

from core import registry
from core.base import CurlTemplate
from core.db import db
from core.schemas.templates import TemplateBody

router = APIRouter()


@router.get(
    "/aggregators/{name}/template",
    tags=["admin"],
    summary="Template curl actif d'un agrégateur (admin)",
    include_in_schema=False,
    responses={404: {"description": "Agrégateur inconnu ou aucun template actif."}},
)
async def get_template(name: str):
    """Renvoie le template de replay actif de l'agrégateur (404 si aucun)."""
    if registry.get(name) is None:
        raise HTTPException(404, {"error": "unknown_aggregator", "supported_aggregators": registry.names()})
    tpl = await db.load_template(name)
    if tpl is None:
        raise HTTPException(404, f"No active template for '{name}'. Run mode='browser' or POST one.")
    return {"aggregator": name, "template": dataclasses.asdict(tpl)}


@router.post(
    "/aggregators/{name}/template",
    tags=["admin"],
    summary="Ajouter/mettre à jour manuellement le template curl (admin)",
    include_in_schema=False,
    responses={
        404: {"description": "Agrégateur inconnu."},
        503: {"description": "Supabase non configuré (persistance désactivée)."},
    },
)
async def set_template(name: str, body: TemplateBody):
    """Ajoute manuellement un template de replay pour l'agrégateur.

    Par défaut **append-si-différent** (comme le mode browser) : si identique à
    l'actif, rien n'est ajouté. `force=true` crée toujours une nouvelle version.
    """
    if registry.get(name) is None:
        raise HTTPException(404, {"error": "unknown_aggregator", "supported_aggregators": registry.names()})
    if not db.enabled:
        raise HTTPException(503, "Supabase non configuré — impossible de persister le template.")
    fields = {f.name for f in dataclasses.fields(CurlTemplate)}
    tpl = CurlTemplate(**{k: v for k, v in body.model_dump().items() if k in fields})
    saved = await db.save_template(name, tpl, force=body.force)
    return {"aggregator": name, "saved": saved, "template": dataclasses.asdict(tpl)}
