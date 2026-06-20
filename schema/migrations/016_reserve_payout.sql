-- 016_reserve_payout.sql — réservation atomique de solde pour un payout.
--
-- Un retrait ne doit JAMAIS dépasser le solde de l'app (sinon elle retirerait
-- l'argent d'une autre app, cassant l'invariant). Le contrôle de solde ET
-- l'insertion du débit doivent être ATOMIQUES : sinon deux payouts simultanés
-- pourraient chacun lire « solde suffisant » puis débiter au-delà du solde
-- (double-dépense).
--
-- Cette fonction fait les deux dans une seule transaction, sérialisée par un
-- ADVISORY LOCK par app_id (pg_advisory_xact_lock) : deux appels concurrents pour
-- la même app s'exécutent l'un après l'autre, donc le second voit le débit du
-- premier. Retourne un JSON {ok, balance, balance_after}.
--   ok=true  -> débit inséré, balance_after = solde restant.
--   ok=false -> rien inséré (solde insuffisant), balance = solde disponible.
--
-- Idempotent : create or replace.

create or replace function reserve_payout(
    p_app_id          bigint,
    p_amount          integer,
    p_transaction_id  bigint,
    p_currency        text default 'XAF'
) returns jsonb
language plpgsql
as $$
declare
    v_balance integer;
begin
    -- Sérialise les payouts concurrents de CETTE app (libéré en fin de transaction).
    perform pg_advisory_xact_lock(p_app_id);

    select coalesce(sum(case when direction = 'credit' then amount else -amount end), 0)
      into v_balance
      from app_ledger
     where app_id = p_app_id;

    if v_balance < p_amount then
        return jsonb_build_object('ok', false, 'balance', v_balance);
    end if;

    -- Le prédicat (where transaction_id is not null) est OBLIGATOIRE pour que
    -- Postgres infère l'index UNIQUE PARTIEL app_ledger_tx_direction_idx.
    insert into app_ledger (app_id, direction, amount, currency, transaction_id, reason)
    values (p_app_id, 'debit', p_amount, p_currency, p_transaction_id, 'payout')
    on conflict (transaction_id, direction) where transaction_id is not null do nothing;

    return jsonb_build_object('ok', true, 'balance', v_balance,
                              'balance_after', v_balance - p_amount);
end;
$$;
