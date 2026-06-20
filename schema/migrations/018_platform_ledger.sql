-- 018_platform_ledger.sql — solde PLATEFORME (argent propre de MobileWallet).
--
-- Distinct des soldes des apps clientes (app_ledger) : c'est l'argent qui
-- appartient à MobileWallet lui-même (marge, frais perçus, flottant). L'admin
-- peut le RECHARGER (credit) et RETIRER dessus (debit, via un payout plateforme).
--
-- À la différence d'app_ledger, le payout plateforme n'est PAS bloqué par le
-- solde (admin de confiance) : le solde peut devenir négatif, puis être rechargé
-- via POST /admin/platform/credit.
--
-- Invariant global étendu :
--   Σ soldes des apps + solde plateforme == solde réel du compte global DigiKUNTZ.
--
-- Idempotent.

create table if not exists platform_ledger (
    id              bigint generated always as identity primary key,
    direction       text        not null check (direction in ('credit', 'debit')),
    amount          integer     not null check (amount > 0),
    currency        text        not null default 'XAF',
    transaction_id  bigint      references transactions(id),  -- payout plateforme source
    reason          text,                                     -- 'reload' | 'payout' | 'payout_refund'
    created_at      timestamptz not null default now()
);

-- Un débit par transaction de payout (idempotence du remboursement éventuel).
create unique index if not exists platform_ledger_tx_direction_idx
    on platform_ledger (transaction_id, direction)
    where transaction_id is not null;

-- Vue solde dérivé (un seul solde, pas de clé) : credits positifs, debits négatifs.
create or replace view platform_balance as
select coalesce(sum(case when direction = 'credit' then amount else -amount end), 0) as balance
from platform_ledger;
