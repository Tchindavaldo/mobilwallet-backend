-- 027 : détail figé du calcul sur chaque ligne de app_ledger.
--
-- Copie, AU MOMENT de l'écriture, du brut, du réseau et de chaque frais
-- (montant en XAF + pourcentage appliqué). Changer les taux plus tard ne fausse
-- donc pas les lignes passées. Colonnes NULL sur les lignes antérieures
-- (details_reconstructed = true si remplies après coup).

alter table app_ledger add column if not exists network                 text;
alter table app_ledger add column if not exists operation               text;  -- deposit | payout
alter table app_ledger add column if not exists gross                   integer;
alter table app_ledger add column if not exists fee_payer               text;
alter table app_ledger add column if not exists deposit_fee             integer;
alter table app_ledger add column if not exists deposit_fee_rate        numeric(10,6);
alter table app_ledger add column if not exists payout_fee_reserve      integer;
alter table app_ledger add column if not exists payout_fee              numeric(18,6);
alter table app_ledger add column if not exists payout_fee_from_reserve numeric(18,6);
alter table app_ledger add column if not exists payout_fee_rate         numeric(10,6);
alter table app_ledger add column if not exists mw_commission           integer;
alter table app_ledger add column if not exists mw_commission_type      text;  -- percent | flat
alter table app_ledger add column if not exists mw_commission_rate      numeric(10,6);
alter table app_ledger add column if not exists details_reconstructed   boolean not null default false;

create index if not exists app_ledger_app_created_idx on app_ledger (app_id, created_at desc);
