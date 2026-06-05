"""Modèles request/response du domaine templates curl."""

from pydantic import BaseModel, Field


class TemplateBody(BaseModel):
    """Champs du curl template (tous optionnels — seuls ceux fournis sont posés)."""
    charge_url: str = ""
    verify_url: str = ""
    init_url: str = ""
    upgrade_url: str = ""
    hosted_pay_url: str = ""
    headers: dict = Field(default_factory=dict)
    payload_skeleton: dict = Field(default_factory=dict)
    public_key_rsa: str = ""
    flw_pub_key: str = ""
    force: bool = Field(False, description="Forcer une nouvelle version même si identique à l'actif.")
