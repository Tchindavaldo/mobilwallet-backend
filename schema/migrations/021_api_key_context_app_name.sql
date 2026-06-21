-- Migration 021 : ajouter apps.name dans la vue api_key_context.
-- Permet de résoudre le nom de l'app à l'authentification (AuthContext.app_name)
-- pour l'utiliser comme libellé dans les appels DigiKUNTZ (raisonForTransfer).

create or replace view api_key_context as
select
    k.id             as api_key_id,
    k.env            as env,
    k.key_hash       as key_hash,
    k.is_active      as key_active,
    a.id             as app_id,
    a.name           as app_name,
    a.callback_url   as callback_url,
    a.webhook_secret as webhook_secret,
    a.is_active      as app_active,
    d.id             as developer_id,
    d.is_active      as dev_active
from api_keys k
    join apps a       on a.id = k.app_id
    join developers d on d.id = a.developer_id;
