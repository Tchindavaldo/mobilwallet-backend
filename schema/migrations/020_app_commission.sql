-- Migration 020 : commission MobileWallet variable par app.
-- NULL = pas de surcharge → l'app tombe sur la config par défaut de l'agrégateur
-- (table aggregators). Non-NULL = commission spécifique négociée pour cette app.

alter table apps
  add column if not exists mw_commission_type  text
    check (mw_commission_type in ('percent', 'flat')),
  add column if not exists mw_commission_value numeric(12,4);
