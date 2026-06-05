"""Modèles request/response du domaine dev (pilotage libre)."""

from pydantic import BaseModel


class DriveRequest(BaseModel):
    url: str
    objective: str
    phone: str = ""
    network: str = ""
