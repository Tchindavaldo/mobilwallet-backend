-- 031 : un payin réussi entièrement absorbé par les frais (crédit app = 0)
-- garde sa ligne dans app_ledger, avec le détail du calcul : amount >= 0.
alter table app_ledger drop constraint if exists app_ledger_amount_check;
alter table app_ledger add constraint app_ledger_amount_check check (amount >= 0);
