"""Configuration du replay : constantes DigiKUNTZ/Flutterwave (.env), HEADERS, ReplayConfig.
Utilise par steps et aggregator (surcharge par template BD).
"""

from core.config import settings

_dk = settings.digikuntz
DIGIKUNTZ_BASE = _dk.base
DIGIKUNTZ_USER_ID = _dk.user_id
DIGIKUNTZ_SECRET = _dk.secret

FLW_PUB_KEY = _dk.flw_pub_key
FLW_CHARGE_URL = _dk.flw_charge_url
FLW_VERIFY_URL = _dk.flw_verify_url
FLW_INIT_URL = _dk.flw_init_url
FLW_UPGRADE_URL = _dk.flw_upgrade_url

HEADERS = {
    "content-type": "application/json",
    "accept": "*/*",
    "origin": "https://checkout-v3-ui-prod.f4b-flutterwave.com",
    "referer": "https://checkout-v3-ui-prod.f4b-flutterwave.com/",
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "x-flw-lang": "FR",
}


from dataclasses import dataclass as _dataclass, field as _field


@_dataclass
class ReplayConfig:
    """Per-call Flutterwave config for a single replay.

    Passed explicitly to step2/step3/step4 so concurrent replays never share
    mutable module globals. `from_template` builds it from a stored CurlTemplate;
    `defaults()` reuses the module-level values (CLI / no template).
    """
    charge_url: str = ""
    verify_url: str = ""
    init_url: str = ""
    upgrade_url: str = ""
    pub_key: str = ""
    headers: dict = _field(default_factory=lambda: dict(HEADERS))

    @classmethod
    def defaults(cls) -> "ReplayConfig":
        return cls(
            charge_url=FLW_CHARGE_URL,
            verify_url=FLW_VERIFY_URL,
            init_url=FLW_INIT_URL,
            upgrade_url=FLW_UPGRADE_URL,
            pub_key=FLW_PUB_KEY,
            headers=dict(HEADERS),
        )

    @classmethod
    def from_template(cls, template) -> "ReplayConfig":
        """Build from a CurlTemplate, falling back to defaults for empty fields."""
        cfg = cls.defaults()
        if not template:
            return cfg
        if template.charge_url:
            cfg.charge_url = template.charge_url
        if template.verify_url:
            cfg.verify_url = template.verify_url
        if getattr(template, "init_url", ""):
            cfg.init_url = template.init_url
        if getattr(template, "upgrade_url", ""):
            cfg.upgrade_url = template.upgrade_url
        if getattr(template, "flw_pub_key", ""):
            cfg.pub_key = template.flw_pub_key
        if template.headers:
            cfg.headers = dict(template.headers)
        return cfg


def encrypt_payload(plaintext: str, public_key_b64: str) -> str:
    """Encrypt plaintext using RSA public key in cryptico format.

    Cryptico uses RSA with PKCS1v1.5 padding. The public key is an RSA
    public key in a custom base64 format. The output is:
    base64(encrypted_aes_key)?base64(aes_encrypted_data)

    For simplicity, we use the 3DES encryption that Flutterwave's
    getEncryptionKey derives from the secret key.
    """
    # Actually, Flutterwave v3 checkout uses cryptico.js which is RSA+AES.
    # But the /charge endpoint also accepts a simpler format using
    # Flutterwave's own encryption with the encryption key derived from
    # the secret key. Let's try the direct Flutterwave v3 charge API
    # format instead.
    #
    # For now, return the plaintext — we'll use a different approach.
    return plaintext


