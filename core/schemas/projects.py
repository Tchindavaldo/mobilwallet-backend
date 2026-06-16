"""Modèles request du domaine projets (apps gérées par le developer)."""

from pydantic import BaseModel, Field


class AppCreateSelf(BaseModel):
    """Création d'une app par le dev (le developer_id vient du JWT, pas du corps)."""
    name: str = Field(..., description="Nom de l'app/projet.", examples=["Acme Checkout"])
