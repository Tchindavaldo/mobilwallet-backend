-- Migration 019 : table de configuration des agrégateurs.
-- Stocke les taux de commission de l'agrégateur (ex. DigiKUNTZ 5%) et la
-- commission MobileWallet (pourcentage ou montant fixe XAF) par agrégateur.
-- Dynamique : ajouter un nouvel agrégateur = une ligne, pas de code.

create table if not exists aggregators (
    name                  text        primary key,        -- 'digikuntz'
    display_name          text        not null,           -- 'DigiKUNTZ'
    -- Taux prélevé par l'agrégateur sur le brut (ex. 0.05 = 5%).
    -- Sert à calculer le net avant d'appliquer la commission MobileWallet.
    aggregator_fee_rate   numeric(6,4) not null default 0,
    -- Commission MobileWallet prélevée sur le NET (après frais agrégateur).
    -- type : 'percent' (valeur = taux, ex. 0.05) | 'flat' (valeur = XAF fixe)
    mw_commission_type    text        not null default 'percent'
                            check (mw_commission_type in ('percent', 'flat')),
    mw_commission_value   numeric(12,4) not null default 0,
    active                boolean     not null default true,
    created_at            timestamptz not null default now()
);

-- Pré-remplir DigiKUNTZ : 5% agrégateur, 5% MobileWallet sur le net.
insert into aggregators (name, display_name, aggregator_fee_rate, mw_commission_type, mw_commission_value)
values ('digikuntz', 'DigiKUNTZ', 0.05, 'percent', 0.05)
on conflict (name) do nothing;
