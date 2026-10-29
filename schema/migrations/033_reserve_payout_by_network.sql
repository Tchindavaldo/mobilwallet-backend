-- 033 : réservation atomique d'un payout d'app débité d'un SOLDE OPÉRATEUR.
-- (Fonction SQL : à coller dans Supabase -> SQL Editor, le MCP part en timeout.)
--
-- p_source  : solde opérateur débité ('mtn' | 'orange'), choisi par l'app.
-- p_network : réseau de destination du virement ('mtn' | 'orange').
-- p_rates   : taux de payout par réseau, ex. {"orange": 0.01, "mtn": 0.013}.
--
-- Même réseau (source = destination) : les frais sont pris sur la réserve de ce
--   réseau au centime (prépayée au dépôt) ; si elle ne couvre pas tout, le reste
--   est pris sur le solde (ceil). Le bénéficiaire reçoit le montant, sans frais
--   visibles pour l'app tant que la réserve suffit.
-- Interréseau (source <> destination) : frais ceil(taux destination x montant)
--   pris sur le solde source ; la réserve source liée au montant est libérée.
-- Si le solde source est vidé, sa réserve restante est libérée aussi.

create or replace function reserve_payout_by_network(
    p_app_id          bigint,
    p_aggregator      text,
    p_amount          integer,
    p_source          text,
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
    v_rs         numeric := coalesce((p_rates ->> p_source)::numeric, 0);
    v_res_n      numeric := 0;
    v_res_s      numeric := 0;
    v_covered    numeric := 0;
    v_fee_res    numeric := 0;
    v_fee_main   integer := 0;
    v_from_main  integer;
    v_release    numeric := 0;
begin
    perform pg_advisory_xact_lock(p_app_id);
    select coalesce(sum(case when direction = 'credit' then amount else -amount end), 0)
      into v_balance from app_ledger
     where app_id = p_app_id and aggregator = p_aggregator and balance_network = p_source;
    select coalesce(sum(case when direction = 'credit' then amount else -amount end), 0)
      into v_res_s from app_reserve_ledger
     where app_id = p_app_id and aggregator = p_aggregator and network = p_source;

    if p_source = p_network then
        v_res_n := v_res_s;
        if v_rn > 0 then
            v_covered := least(p_amount::numeric, greatest(v_res_n, 0) / v_rn);
            v_fee_res := v_rn * v_covered;                       -- exact, au centime
            v_fee_main := ceil(v_rn * (p_amount - v_covered));   -- reste, arrondi
        end if;
    else
        v_fee_main := ceil(v_rn * p_amount);
        if v_rs > 0 then
            v_release := least(greatest(v_res_s, 0), v_rs * p_amount);
        end if;
    end if;
    v_from_main := p_amount + v_fee_main;

    if p_dry_run or v_balance < v_from_main then
        return jsonb_build_object(
            'ok', v_balance >= v_from_main, 'dry_run', p_dry_run,
            'balance', v_balance, 'needed', v_from_main,
            'fee_from_balance', v_fee_main, 'fee_from_reserve', round(v_fee_res, 2),
            'balance_after', v_balance - v_from_main);
    end if;

    -- Solde source vidé : toute sa réserve restante est libérée.
    if v_balance - v_from_main = 0 then
        v_release := greatest(v_res_s - v_fee_res, 0);
    end if;

    insert into app_ledger (app_id, direction, amount, currency, transaction_id, reason,
                            aggregator, balance_network)
    values (p_app_id, 'debit', v_from_main, p_currency, p_transaction_id, 'payout',
            p_aggregator, p_source)
    on conflict (transaction_id, direction) where transaction_id is not null do nothing;
    if v_fee_res > 0 then
        insert into app_reserve_ledger (app_id, aggregator, network, direction, amount,
                                        currency, transaction_id, reason)
        values (p_app_id, p_aggregator, p_network, 'debit', v_fee_res, p_currency,
                p_transaction_id, 'payout_fee')
        on conflict do nothing;
    end if;
    if round(v_release, 6) > 0 then
        insert into app_reserve_ledger (app_id, aggregator, network, direction, amount,
                                        currency, transaction_id, reason)
        values (p_app_id, p_aggregator, p_source, 'debit', round(v_release, 6), p_currency,
                p_transaction_id, 'release')
        on conflict do nothing;
    end if;
    return jsonb_build_object(
        'ok', true, 'dry_run', false, 'balance', v_balance, 'needed', v_from_main,
        'fee_from_balance', v_fee_main, 'fee_from_reserve', round(v_fee_res, 2),
        'balance_after', v_balance - v_from_main);
end;
$$;
