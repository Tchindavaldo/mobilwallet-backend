"""Modèles request/response du domaine système/config."""

from pydantic import BaseModel, Field


class MaxTabsRequest(BaseModel):
    max_tabs: int = Field(..., ge=1, le=200,
                          description="Nb max d'onglets par instance Chrome.",
                          examples=[20])
