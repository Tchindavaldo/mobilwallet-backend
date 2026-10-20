-- 030 : relevés pawaPay, UNE ligne par transaction (remplace 028 lignes CSV brutes).
-- Le CSV pawaPay donne 3 lignes par opération (montant, frais pawaPay, frais
-- opérateur) : on les regroupe, chaque frais dans sa colonne.
-- pawapay_statement_lines (028, jamais alimentée) est supprimée.

create table if not exists pawapay_statement_transactions (
    id                       bigint generated always as identity primary key,
    provider_transaction_id  text          not null unique,  -- depositId / payoutId
    occurred_at              timestamptz   not null,         -- "Creation date" (UTC)
    operation                text          not null,         -- Deposit | Payout | Refund...
    msisdn                   text,
    mno                      text,                           -- ex. ORANGE CMR
    amount                   numeric(18,2) not null,         -- montant de l'opération
    pawapay_fee              numeric(18,2) not null default 0,  -- frais pris par pawaPay
    operator_fee             numeric(18,2) not null default 0,  -- frais opérateur (pass through)
    balance_after            numeric(18,2),                  -- solde du wallet après l'opération
    reason                   text,
    metadata                 text,
    country                  text          not null default 'CMR',
    currency                 text          not null default 'XAF',
    statement_id             text,
    synced_at                timestamptz   not null default now()
);
create index if not exists pawapay_statement_transactions_time_idx
    on pawapay_statement_transactions (occurred_at desc);

drop table if exists pawapay_statement_lines;
