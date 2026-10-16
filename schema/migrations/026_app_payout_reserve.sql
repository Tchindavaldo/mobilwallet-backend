-- 026 : réserve de frais de payout PAR APP (au lieu du solde plateforme).
--
-- Au dépôt (fee_payer='app' + prepay_payout_fee), la part des frais de retrait
-- prépayés est créditée dans app_reserve_ledger de l'app, par agrégateur. Au
-- payout, les frais sont pris d'abord sur cette réserve, le manque éventuel sur
-- le solde principal de l'app. La plateforme ne garde que sa commission et les
-- surplus d'arrondi. Idempotent.

create table if not exists app_reserve_ledger (
    id              bigint generated always as identity primary key,
    app_id          bigint      not null references apps(id) on delete cascade,
    aggregator      text        not null,
    direction       text        not null check (direction in ('credit', 'debit')),
    amount          integer     not null check (amount > 0),
    currency        text        not null default 'XAF',
    transaction_id  bigint      references transactions(id),
    reason          text,       -- 'payout_fee_reserve' | 'payout_fee' | 'payout_refund'
    created_at      timestamptz not null default now()
);
create unique index if not exists app_reserve_ledger_tx_direction_idx
    on app_reserve_ledger (transaction_id, direction) where transaction_id is not null;
create index if not exists app_reserve_ledger_app_agg_idx on app_reserve_ledger (app_id, aggregator);

create or replace view app_reserve_by_aggregator as
select app_id, aggregator,
       coalesce(sum(case when direction = 'credit' then amount else -amount end), 0) as balance
from app_reserve_ledger group by app_id, aggregator;

-- Réservation atomique d'un payout d'app : montant sur le solde principal,
-- frais sur la réserve (manque éventuel sur le solde principal).
create or replace function reserve_payout_with_fee(
    p_app_id          bigint,
    p_amount          integer,
    p_fee             integer,
    p_transaction_id  bigint,
    p_currency        text default 'XAF'
) returns jsonb
language plpgsql
as $$
declare
    v_aggregator  text;
    v_balance     integer;
    v_reserve     integer;
    v_from_res    integer;
    v_from_main   integer;
begin
    perform pg_advisory_xact_lock(p_app_id);
    select aggregator into v_aggregator from transactions where id = p_transaction_id;
    select coalesce(sum(case when direction = 'credit' then amount else -amount end), 0)
      into v_balance from app_ledger
     where app_id = p_app_id and aggregator = v_aggregator;
    select coalesce(sum(case when direction = 'credit' then amount else -amount end), 0)
      into v_reserve from app_reserve_ledger
     where app_id = p_app_id and aggregator = v_aggregator;
    v_from_res  := least(greatest(p_fee, 0), greatest(v_reserve, 0));
    v_from_main := p_amount + greatest(p_fee, 0) - v_from_res;
    if v_balance < v_from_main then
        return jsonb_build_object('ok', false, 'balance', v_balance, 'needed', v_from_main,
                                  'reserve', v_reserve, 'aggregator', v_aggregator);
    end if;
    insert into app_ledger (app_id, direction, amount, currency, transaction_id, reason, aggregator)
    values (p_app_id, 'debit', v_from_main, p_currency, p_transaction_id, 'payout', v_aggregator)
    on conflict (transaction_id, direction) where transaction_id is not null do nothing;
    if v_from_res > 0 then
        insert into app_reserve_ledger (app_id, aggregator, direction, amount, currency,
                                        transaction_id, reason)
        values (p_app_id, v_aggregator, 'debit', v_from_res, p_currency,
                p_transaction_id, 'payout_fee')
        on conflict (transaction_id, direction) where transaction_id is not null do nothing;
    end if;
    return jsonb_build_object('ok', true, 'balance', v_balance,
                              'balance_after', v_balance - v_from_main,
                              'from_reserve', v_from_res, 'from_main', v_from_main,
                              'aggregator', v_aggregator);
end;
$$;
