-- Migration 022 : déclare l'agrégateur pawaPay dans la table de configuration.
-- Commission MobileWallet à 0 par défaut : à ajuster côté Supabase selon la
-- politique tarifaire (pawaPay prélève ses frais sur notre wallet pawaPay).
insert into aggregators (name, display_name, aggregator_fee_rate, mw_commission_type, mw_commission_value)
values ('pawapay', 'pawaPay', 0, 'flat', 0)
on conflict (name) do nothing;
