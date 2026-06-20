-- 015_app_ledger.sql — comptabilité par app (grand livre des mouvements).
--
-- MobileWallet est lui-même un agrégateur : DigiKUNTZ n'a qu'UN compte global où
-- atterrissent tous les payins de toutes les apps. C'est donc à NOUS de tenir le
-- solde de chaque app. On le fait par un GRAND LIVRE (ledger) : une ligne par
-- mouvement.
--   - credit : un payin réussi de l'app (argent encaissé pour elle),
--   - debit  : un payout (retrait) de l'app (argent qui sort pour elle).
-- Le solde d'une app = SUM(credit) - SUM(debit). Source de vérité unique,
-- auditable. Invariant : Σ soldes des apps == solde réel du compte global DigiKUNTZ.
--
-- Idempotence : la contrainte unique (transaction_id, direction) garantit qu'un
-- même payin ne crédite qu'UNE fois même si le polling ET le webhook settlent
-- ensemble (course gérée comme pour les notifications). Idem pour un payout/débit.
-- Idempotent : exécutable sans risque même si déjà appliquée.

create table if not exists app_ledger (
    id              bigint generated always as identity primary key,
    app_id          bigint      not null references apps(id) on delete cascade,
    direction       text        not null check (direction in ('credit', 'debit')),
    amount          integer     not null check (amount > 0),
    currency        text        not null default 'XAF',
    transaction_id  bigint      references transactions(id),  -- payin/payout source
    reason          text,                                     -- 'payin_successful' | 'payout' | 'payout_refund'
    created_at      timestamptz not null default now()
);

create index if not exists app_ledger_app_idx on app_ledger (app_id);

-- Un mouvement par (transaction, sens) : empêche tout double crédit/débit.
create unique index if not exists app_ledger_tx_direction_idx
    on app_ledger (transaction_id, direction)
    where transaction_id is not null;

-- Vue solde dérivé : credits positifs, debits négatifs.
create or replace view app_balance as
select
    app_id,
    coalesce(sum(case when direction = 'credit' then amount else -amount end), 0) as balance
from app_ledger
group by app_id;
