-- 011_webhook_deliveries.sql — journal d'envoi des webhooks de verdict.
--
-- Quand une transaction settle (terminal), le backend notifie l'app via son
-- callback_url. Cette table trace chaque envoi et GARANTIT l'idempotence : la
-- contrainte unique (transaction_id, event) empêche d'envoyer deux fois le même
-- verdict, même si le polling ET le webhook DigiKUNTZ settlent presque ensemble
-- (le premier insère la ligne ; le second voit le conflit et n'envoie pas).
--
-- Idempotent : exécutable sans risque même si déjà appliquée.

create table if not exists webhook_deliveries (
    id              bigint generated always as identity primary key,
    transaction_id  bigint      not null references transactions(id) on delete cascade,
    app_id          bigint      references apps(id) on delete cascade,
    event           text        not null,            -- transaction.successful|failed|cancelled|expired
    status          text        not null default 'pending',  -- pending|delivered|failed
    attempts        integer     not null default 0,
    last_error      text,
    delivered_at    timestamptz,
    created_at      timestamptz not null default now()
);

-- Idempotence : un seul envoi par (transaction, event).
create unique index if not exists webhook_deliveries_unique
    on webhook_deliveries (transaction_id, event);
