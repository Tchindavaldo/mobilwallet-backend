-- 028 (table des lignes remplacée par 030, une ligne par transaction) : relevés pawaPay (POST /v2/statements) stockés chez nous = notre trace
-- de l'argent réellement encaissé/décaissé sur le wallet pawaPay.
-- Préfixe `pawapay_` : table propre à l'agrégateur (règle CLAUDE.md).
-- Une ligne CSV = un mouvement (montant, frais pawaPay, frais opérateur...).
-- Idempotent : une ligne déjà connue n'est pas réinsérée (recouvrement entre
-- deux synchros pour rattraper les lignes arrivées en retard).

create table if not exists pawapay_statement_lines (
    id              bigint generated always as identity primary key,
    occurred_at     timestamptz   not null,          -- "Creation date" (UTC)
    msisdn          text,
    provider_transaction_id text  not null,          -- depositId / payoutId
    amount          numeric(18,2) not null,
    mno             text,                            -- ex. ORANGE CMR
    line_type       text          not null,          -- Deposit, Deposit pawaPay fees...
    balance         numeric(18,2),                   -- solde du wallet après la ligne
    reason          text,
    metadata        text,
    country         text          not null default 'CMR',
    currency        text          not null default 'XAF',
    statement_id    text,
    synced_at       timestamptz   not null default now()
);
create unique index if not exists pawapay_statement_lines_uniq
    on pawapay_statement_lines (provider_transaction_id, line_type, occurred_at, amount);
create index if not exists pawapay_statement_lines_time_idx
    on pawapay_statement_lines (occurred_at desc);

-- Historique des synchros (dernière fin couverte = point de départ suivant).
create table if not exists pawapay_statement_syncs (
    id            bigint generated always as identity primary key,
    statement_id  text,
    start_date    timestamptz not null,
    end_date      timestamptz not null,
    status        text        not null,   -- COMPLETED | FAILED | REJECTED
    lines         integer     not null default 0,
    inserted      integer     not null default 0,
    error         text,
    created_at    timestamptz not null default now()
);
