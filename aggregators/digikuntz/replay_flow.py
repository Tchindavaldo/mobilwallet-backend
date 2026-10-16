"""Curl replay: pay via Flutterwave API directly without browser.

Flow:
1. Create digikuntz transaction → get txRef + amount
2. Initialize checkout → get modalauditid
3. Encrypt payload with RSA (cryptico format)
4. POST /charge → get flw_ref
5. Poll /verify/mpesa → get final status
"""


# Facade : le code vit dans replay/ ; ces re-exports gardent `replay_flow.X` valide.
from .replay.interpret import *  # noqa: F401,F403
from .replay.config import *  # noqa: F401,F403
from .replay.config import ReplayConfig, HEADERS, encrypt_payload  # noqa: F401
from .replay.steps import (  # noqa: F401
    step1_create_transaction,
    step2_initialize_checkout,
    step3_charge,
    step4_poll_verify,
)
from .replay.cli import main  # noqa: F401

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
