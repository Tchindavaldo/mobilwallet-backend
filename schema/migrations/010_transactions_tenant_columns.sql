-- 010_transactions_tenant_columns.sql — rattachement tenant des transactions.
--
-- Chaque paiement est désormais rattaché à l'app qui l'a déclenché (via la clé
-- API authentifiée), à la clé exacte utilisée, et porte un identifiant opaque de
-- l'utilisateur final de l'app (fourni par le client, facultatif). Permet :
--   - l'isolation (un dev ne voit que les transactions de ses apps),
--   - la traçabilité (quelle clé/app/end-user pour chaque paiement).
-- developer_id n'est PAS stocké ici : il se déduit via apps.developer_id.
-- Colonnes nullable (transactions historiques + appels sans Supabase). Idempotent.

alter table transactions add column if not exists app_id       bigint references apps(id);
alter table transactions add column if not exists api_key_id   bigint references api_keys(id);
alter table transactions add column if not exists end_user_ref text;   -- opaque, fourni par le client

create index if not exists transactions_app_idx on transactions (app_id, created_at desc);
