-- 009_multitenant_tables.sql — modèle multi-tenant (developers / apps / api_keys).
--
-- Transforme le backend en agrégateur contrôlé : des devs intégrateurs
-- s'authentifient par clé API. Hiérarchie :
--   developers (compte)  ──<  apps (chaque app d'un dev)  ──<  api_keys (par app)
-- Une app porte son callback_url (webhook serveur-à-serveur) et son webhook_secret
-- (HMAC). Une clé existe en deux environnements : 'test' | 'live'.
--
-- SÉCURITÉ : la clé en clair n'est JAMAIS stockée. On garde :
--   key_hash   = sha256(clé complète)  -> lookup O(1) d'une clé entrante,
--   key_prefix = "sk_test_xxxxxx"      -> affichable (non secret),
--   last_four  = 4 derniers caractères -> affichage.
-- La clé complète n'est renvoyée qu'UNE fois, à la création (endpoint admin).
--
-- Idempotent : exécutable sans risque même si déjà appliquée.

create table if not exists developers (
    id          bigint generated always as identity primary key,
    email       text        not null unique,
    name        text,
    is_active   boolean     not null default true,
    created_at  timestamptz not null default now()
);

create table if not exists apps (
    id              bigint generated always as identity primary key,
    developer_id    bigint      not null references developers(id) on delete cascade,
    name            text        not null,
    callback_url    text,                            -- webhook serveur-à-serveur du verdict
    webhook_secret  text        not null,            -- secret HMAC (généré, jamais ré-affiché)
    is_active       boolean     not null default true,
    created_at      timestamptz not null default now()
);
create index if not exists apps_developer_idx on apps (developer_id);

create table if not exists api_keys (
    id          bigint generated always as identity primary key,
    app_id      bigint      not null references apps(id) on delete cascade,
    env         text        not null check (env in ('test', 'live')),
    key_prefix  text        not null,                -- ex. "sk_test_a1b2c3" (affichable)
    key_hash    text        not null,                -- sha256 de la clé complète
    last_four   text,                                -- 4 derniers chars, pour l'affichage
    is_active   boolean     not null default true,
    revoked_at  timestamptz,
    created_at  timestamptz not null default now()
);
create index if not exists api_keys_app_idx on api_keys (app_id);
-- Résolution O(1) d'une clé entrante par son hash (clés actives uniquement).
create unique index if not exists api_keys_hash_idx
    on api_keys (key_hash) where is_active;

-- Vue de résolution : à partir d'une clé (key_hash), retrouve en UNE requête
-- l'app + le developer + l'état d'activité de chaque niveau. C'est elle que lit
-- le backend (le dev ne passe que sa clé ; developer_id en est déduit ici, pas
-- dupliqué sur api_keys).
create or replace view api_key_context as
select
    k.id            as api_key_id,
    k.env           as env,
    k.key_hash      as key_hash,
    k.is_active     as key_active,
    a.id            as app_id,
    a.callback_url  as callback_url,
    a.webhook_secret as webhook_secret,
    a.is_active     as app_active,
    d.id            as developer_id,
    d.is_active     as dev_active
from api_keys k
    join apps a       on a.id = k.app_id
    join developers d on d.id = a.developer_id;
