-- 026 : réserve de frais de payout PAR APP et PAR RÉSEAU, comptée au centime.
--
-- Au dépôt (fee_payer='app' + prepay_payout_fee), on retient ceil(brut x taux de
-- payout du réseau du dépôt) dans la réserve de l'app pour CE réseau.
-- Au payout de X vers le réseau n (taux r_n) :
--   - partie couverte C = min(X, réserve_n / r_n) : frais EXACTS r_n x C pris
--     sur la réserve n (au centime, jamais arrondis -> la réserve couvre toujours) ;
--   - partie non couverte U = X - C (fonds venus d'un autre réseau m) : frais
--     ceil(r_n x U) pris sur le solde de l'app, et la réserve m liée à U
--     (r_m x U) est libérée -> reversée à la plateforme (francs entiers, par le
--     reversement de trésorerie).
-- Si le payout vide le solde, la réserve restante est libérée aussi.
-- Un payout échoué rend exactement chaque débit. Idempotent.

create table if not exists app_reserve_ledger (
    id              bigint generated always as identity primary key,
    app_id          bigint        not null references apps(id) on delete cascade,
    aggregator      text          not null,
    network         text          not null default '',
    direction       text          not null check (direction in ('credit', 'debit')),
    amount          numeric(18,6) not null check (amount > 0),
    currency        text          not null default 'XAF',
    transaction_id  bigint        references transactions(id),
    -- credit : 'deposit_reserve' | 'payout_refund' | 'release_refund'
    -- debit  : 'payout_fee' | 'release'
    reason          text          not null,
    created_at      timestamptz   not null default now()
);
create unique index if not exists app_reserve_ledger_tx_idx
    on app_reserve_ledger (transaction_id, direction, network, reason)
    where transaction_id is not null;
create index if not exists app_reserve_ledger_app_agg_idx
    on app_reserve_ledger (app_id, aggregator, network);

create or replace view app_reserve_by_network as
select app_id, aggregator, network,
       coalesce(sum(case when direction = 'credit' then amount else -amount end), 0) as balance
from app_reserve_ledger group by app_id, aggregator, network;

-- Réserve libérée (à reverser à la plateforme), par agrégateur.
create or replace view app_reserve_released_by_aggregator as
select aggregator,
       coalesce(sum(case when reason = 'release' then amount
                         when reason = 'release_refund' then -amount else 0 end), 0) as released
from app_reserve_ledger group by aggregator;

drop function if exists reserve_payout_with_fee(bigint, integer, integer, bigint, text);

-- Calcule (p_dry_run) ou réserve atomiquement un payout d'app.
-- p_rates : taux de payout par réseau, ex. {"orange": 0.01, "mtn": 0.013}.
create or replace function reserve_payout_with_fee(
    p_app_id          bigint,
    p_aggregator      text,
    p_amount          integer,
    p_network         text,
    p_rates           jsonb,
    p_transaction_id  bigint  default null,
    p_currency        text    default 'XAF',
    p_dry_run         boolean default false
) returns jsonb
language plpgsql
as $$
declare
    v_balance    integer;
    v_rn         numeric := coalesce((p_rates ->> p_network)::numeric, 0);
    v_res_n      numeric := 0;
    v_covered    numeric;
    v_uncovered  numeric;
    v_fee_res    numeric := 0;
    v_fee_main   integer := 0;
    v_from_main  integer;
    v_left       numeric;
    v_part       numeric;
    v_rm         numeric;
    v_releases   jsonb := '{}'::jsonb;
    r            record;
begin
    perform pg_advisory_xact_lock(p_app_id);
    select coalesce(sum(case when direction = 'credit' then amount else -amount end), 0)
      into v_balance from app_ledger
     where app_id = p_app_id and aggregator = p_aggregator;
    select coalesce(sum(case when direction = 'credit' then amount else -amount end), 0)
      into v_res_n from app_reserve_ledger
     where app_id = p_app_id and aggregator = p_aggregator and network = p_network;

    if v_rn > 0 then
        v_covered := least(p_amount::numeric, greatest(v_res_n, 0) / v_rn);
        v_fee_res := v_rn * v_covered;               -- exact, au centime près
        v_uncovered := p_amount - v_covered;
        v_fee_main := ceil(v_rn * v_uncovered);      -- arrondi à l'excès
    else
        v_uncovered := p_amount;
    end if;
    -- (taux inconnu pour ce réseau : pas de frais imputés, comme avant)
    v_from_main := p_amount + v_fee_main;

    -- Réserve des autres réseaux liée à la partie non couverte -> libérée.
    v_left := case when v_rn > 0 then v_uncovered else 0 end;
    for r in select network, sum(case when direction = 'credit' then amount else -amount end) as bal
               from app_reserve_ledger
              where app_id = p_app_id and aggregator = p_aggregator and network <> p_network
              group by network
    loop
        exit when v_left <= 0;
        v_rm := coalesce((p_rates ->> r.network)::numeric, 0);
        continue when v_rm <= 0 or r.bal <= 0;
        v_part := least(v_left, r.bal / v_rm);
        v_releases := v_releases || jsonb_build_object(r.network, round(v_rm * v_part, 6));
        v_left := v_left - v_part;
    end loop;

    if p_dry_run or v_balance < v_from_main then
        return jsonb_build_object(
            'ok', v_balance >= v_from_main, 'dry_run', p_dry_run,
            'balance', v_balance, 'needed', v_from_main,
            'fee_from_balance', v_fee_main, 'fee_from_reserve', round(v_fee_res, 2),
            'balance_after', v_balance - v_from_main);
    end if;

    insert into app_ledger (app_id, direction, amount, currency, transaction_id, reason, aggregator)
    values (p_app_id, 'debit', v_from_main, p_currency, p_transaction_id, 'payout', p_aggregator)
    on conflict (transaction_id, direction) where transaction_id is not null do nothing;
    if v_fee_res > 0 then
        insert into app_reserve_ledger (app_id, aggregator, network, direction, amount,
                                        currency, transaction_id, reason)
        values (p_app_id, p_aggregator, p_network, 'debit', v_fee_res, p_currency,
                p_transaction_id, 'payout_fee')
        on conflict do nothing;
    end if;
    -- Solde vidé : la réserve restante ne sert plus à rien -> libérée aussi.
    if v_balance - v_from_main = 0 then
        for r in select network, sum(case when direction = 'credit' then amount else -amount end) as bal
                   from app_reserve_ledger
                  where app_id = p_app_id and aggregator = p_aggregator group by network
        loop
            -- (frais de ce payout déjà insérés ci-dessus, libérations pas encore)
            v_part := r.bal - coalesce((v_releases ->> r.network)::numeric, 0);
            if v_part > 0 then
                v_releases := v_releases || jsonb_build_object(r.network,
                    round(coalesce((v_releases ->> r.network)::numeric, 0) + v_part, 6));
            end if;
        end loop;
    end if;
    for r in select key, value::numeric as amt from jsonb_each_text(v_releases) loop
        if r.amt > 0 then
            insert into app_reserve_ledger (app_id, aggregator, network, direction, amount,
                                            currency, transaction_id, reason)
            values (p_app_id, p_aggregator, r.key, 'debit', r.amt, p_currency,
                    p_transaction_id, 'release')
            on conflict do nothing;
        end if;
    end loop;
    return jsonb_build_object(
        'ok', true, 'dry_run', false, 'balance', v_balance, 'needed', v_from_main,
        'fee_from_balance', v_fee_main, 'fee_from_reserve', round(v_fee_res, 2),
        'balance_after', v_balance - v_from_main);
end;
$$;
