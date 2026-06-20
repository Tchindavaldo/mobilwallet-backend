-- 017_transactions_payout.sql — payouts (retraits) dans la table transactions.
--
-- Les retraits sont stockés dans la MÊME table que les payins, distingués par la
-- colonne `type` ('payin' par défaut | 'payout'). Un payout porte les coordonnées
-- du bénéficiaire (pas un payeur) : banque/réseau, numéro, nom, devise, motif.
-- `phone` reste rempli avec account_number pour réutiliser les index/garde existants
-- sur (aggregator, phone) ; account_number garde la valeur explicite côté payout.
--
-- Colonnes nullable (transactions payin historiques + appels sans tenant). Idempotent.

alter table transactions add column if not exists type             text not null default 'payin';
alter table transactions add column if not exists account_bank_code text;
alter table transactions add column if not exists account_number    text;
alter table transactions add column if not exists receiver_name     text;
alter table transactions add column if not exists currency          text;
alter table transactions add column if not exists narration         text;

-- Garde anti-doublon payout : au plus un retrait 'pending' par bénéficiaire.
create index if not exists transactions_payout_pending_idx
    on transactions (aggregator, account_number)
    where type = 'payout' and status = 'pending';
