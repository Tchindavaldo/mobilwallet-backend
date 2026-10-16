-- Migration 024 : soldes (apps + plateforme) séparés PAR AGRÉGATEUR.
--
-- Chaque ligne de app_ledger / platform_ledger porte l'agrégateur dont le compte
-- réel détient l'argent (DigiKUNTZ, pawaPay…). Ainsi :
--   - un écart / des tests sur un agrégateur n'affectent pas les soldes d'un autre ;
--   - la réconciliation compare, agrégateur par agrégateur, les soldes internes
--     au solde réel du compte chez cet agrégateur ;
--   - un payout via un agrégateur ne peut dépenser que l'argent détenu chez lui.
-- Pour une ligne rattachée à une transaction, l'agrégateur est déduit
-- automatiquement de transactions.aggregator (trigger) ; une écriture manuelle
-- (recharge, ajustement) doit le préciser.
-- Idempotent.

alter table app_ledger      add column if not exists aggregator text;
alter table platform_ledger add column if not exists aggregator text;

-- Historique : ligne rattachée -> agrégateur de la transaction ; sinon DigiKUNTZ
-- (seul agrégateur avant pawaPay).
update app_ledger l set aggregator = coalesce(t.aggregator, 'digikuntz')
  from transactions t where l.aggregator is null and l.transaction_id = t.id;
update app_ledger set aggregator = 'digikuntz' where aggregator is null;
update platform_ledger l set aggregator = coalesce(t.aggregator, 'digikuntz')
  from transactions t where l.aggregator is null and l.transaction_id = t.id;
update platform_ledger set aggregator = 'digikuntz' where aggregator is null;

create or replace function ledger_fill_aggregator() returns trigger
language plpgsql as $$
begin
    if new.aggregator is null and new.transaction_id is not null then
        select aggregator into new.aggregator from transactions where id = new.transaction_id;
    end if;
    if new.aggregator is null then
        raise exception 'ledger: aggregator requis (aucune transaction rattachée)';
    end if;
    return new;
end;
$$;

drop trigger if exists app_ledger_fill_aggregator on app_ledger;
create trigger app_ledger_fill_aggregator before insert on app_ledger
    for each row execute function ledger_fill_aggregator();
drop trigger if exists platform_ledger_fill_aggregator on platform_ledger;
create trigger platform_ledger_fill_aggregator before insert on platform_ledger
    for each row execute function ledger_fill_aggregator();

alter table app_ledger      alter column aggregator set not null;
alter table platform_ledger alter column aggregator set not null;
create index if not exists app_ledger_app_agg_idx on app_ledger (app_id, aggregator);

-- Soldes par agrégateur (les vues totales app_balance / platform_balance restent).
create or replace view app_balance_by_aggregator as
select app_id, aggregator,
       coalesce(sum(case when direction = 'credit' then amount else -amount end), 0) as balance
from app_ledger group by app_id, aggregator;

create or replace view platform_balance_by_aggregator as
select aggregator,
       coalesce(sum(case when direction = 'credit' then amount else -amount end), 0) as balance
from platform_ledger group by aggregator;

-- Réservation de payout : le solde contrôlé est celui de l'app CHEZ l'agrégateur
-- du payout (déduit de la transaction).
create or replace function reserve_payout(
    p_app_id          bigint,
    p_amount          integer,
    p_transaction_id  bigint,
    p_currency        text default 'XAF'
) returns jsonb
language plpgsql
as $$
declare
    v_balance    integer;
    v_aggregator text;
begin
    perform pg_advisory_xact_lock(p_app_id);
    select aggregator into v_aggregator from transactions where id = p_transaction_id;

    select coalesce(sum(case when direction = 'credit' then amount else -amount end), 0)
      into v_balance
      from app_ledger
     where app_id = p_app_id and aggregator = v_aggregator;

    if v_balance < p_amount then
        return jsonb_build_object('ok', false, 'balance', v_balance, 'aggregator', v_aggregator);
    end if;

    insert into app_ledger (app_id, direction, amount, currency, transaction_id, reason, aggregator)
    values (p_app_id, 'debit', p_amount, p_currency, p_transaction_id, 'payout', v_aggregator)
    on conflict (transaction_id, direction) where transaction_id is not null do nothing;

    return jsonb_build_object('ok', true, 'balance', v_balance,
                              'balance_after', v_balance - p_amount, 'aggregator', v_aggregator);
end;
$$;
