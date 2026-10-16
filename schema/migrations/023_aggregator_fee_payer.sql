-- Migration 023 : qui paie les frais de l'agrégateur + taux par réseau/opération.
--
-- fee_payer (table aggregators) :
--   'customer' : l'agrégateur prend ses frais directement au client final
--                (ex. DigiKUNTZ via l'USSD) -> hors comptabilité MobileWallet.
--   'app'      : frais prélevés sur le wallet marchand (ex. pawaPay) et
--                répercutés sur l'app cliente (déduits de son crédit / ajoutés
--                au débit d'un payout).
--   'platform' : frais prélevés sur le wallet marchand et supportés par
--                MobileWallet (déduits du solde plateforme).
-- prepay_payout_fee (payer='app') : au dépôt, on retient AUSSI les frais de
--   retrait futurs (gardés en réserve sur le solde plateforme) ; quand l'app
--   retire son solde, les frais de retrait sont déjà payés.
-- Les taux réels vivent dans aggregator_fee_rates (par réseau et opération).
-- Idempotent.

alter table aggregators
    add column if not exists fee_payer text not null default 'customer';

alter table aggregators
    add column if not exists prepay_payout_fee boolean not null default false;

do $$ begin
    alter table aggregators add constraint aggregators_fee_payer_check
        check (fee_payer in ('customer', 'app', 'platform'));
exception when duplicate_object then null; end $$;

create table if not exists aggregator_fee_rates (
    aggregator  text         not null references aggregators(name) on delete cascade,
    network     text         not null check (network in ('mtn', 'orange')),
    operation   text         not null check (operation in ('deposit', 'payout')),
    rate        numeric(6,4) not null default 0 check (rate >= 0 and rate < 1),
    updated_at  timestamptz  not null default now(),
    primary key (aggregator, network, operation)
);

-- Frais réellement prélevés par l'agrégateur sur une transaction (XAF), pour
-- l'audit et pour rembourser exactement ce qui a été débité sur un payout échoué.
alter table transactions add column if not exists provider_fee integer not null default 0;

-- pawaPay prélève sur le wallet marchand : le client de l'app paie les frais de
-- dépôt ET prépaie ceux du retrait (MobileWallet ne paie rien).
update aggregators set fee_payer = 'app', prepay_payout_fee = true where name = 'pawapay';

-- Grille publique pawaPay Cameroun (https://www.pawapay.io/fees, hors taxes,
-- « tiered ») : à ajuster selon le contrat.
insert into aggregator_fee_rates (aggregator, network, operation, rate) values
    ('pawapay', 'mtn',    'deposit', 0.0175),
    ('pawapay', 'orange', 'deposit', 0.0177),
    ('pawapay', 'mtn',    'payout',  0.0130),
    ('pawapay', 'orange', 'payout',  0.0100)
on conflict (aggregator, network, operation) do nothing;
