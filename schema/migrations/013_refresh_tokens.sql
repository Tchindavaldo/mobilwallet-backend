-- 013_refresh_tokens.sql — refresh tokens des sessions developer.
--
-- L'access token (JWT) est court ; le refresh token (long) permet de le renouveler
-- sans re-login, et d'invalider une session (logout) ou de tourner les tokens.
-- On stocke le HASH du refresh (jamais en clair) ; un index unique sur token_hash
-- permet la résolution O(1). revoked_at marque une session close.
--
-- Idempotent : exécutable sans risque même si déjà appliquée.

create table if not exists refresh_tokens (
    id            bigint generated always as identity primary key,
    developer_id  bigint      not null references developers(id) on delete cascade,
    token_hash    text        not null,
    expires_at    timestamptz not null,
    revoked_at    timestamptz,
    created_at    timestamptz not null default now()
);

create unique index if not exists refresh_tokens_hash_idx on refresh_tokens (token_hash);
create index if not exists refresh_tokens_dev_idx on refresh_tokens (developer_id);
