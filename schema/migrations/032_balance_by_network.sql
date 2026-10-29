-- 032 : solde d'app PAR OPÉRATEUR (réseau) en plus du solde par agrégateur.
--
-- Chaque ligne de app_ledger porte `balance_network` = le solde opérateur
-- qu'elle mouvemente ('mtn' | 'orange') :
--   - crédit de dépôt : réseau du dépôt ;
--   - débit de payout : réseau SOURCE choisi par l'app (peut différer du réseau
--     de destination, colonne `network`, en cas d'interréseau) ;
--   - remboursement de payout : même réseau que le débit remboursé.
-- La réserve de frais (app_reserve_ledger) reste invisible pour l'app : le solde
-- d'un opérateur est retirable SANS frais vers ce même opérateur.

alter table app_ledger add column if not exists balance_network text;

create index if not exists app_ledger_app_agg_net_idx
    on app_ledger (app_id, aggregator, balance_network);

create or replace view app_balance_by_network as
select app_id, aggregator, balance_network as network,
       coalesce(sum(case when direction = 'credit' then amount else -amount end), 0) as balance
from app_ledger
where balance_network is not null
group by app_id, aggregator, balance_network;

-- Reprise de l'existant : réseau de la transaction (dépôt = réseau du dépôt ;
-- payout = réseau de destination, seul connu avant cette migration).
update app_ledger l
   set balance_network = case when lower(t.network) like 'orange%' then 'orange'
                              when lower(t.network) like 'mtn%' then 'mtn' end
  from transactions t
 where t.id = l.transaction_id and l.balance_network is null;

-- Lignes sans transaction (ajustements manuels) : solde Orange par défaut.
update app_ledger set balance_network = 'orange' where balance_network is null;
