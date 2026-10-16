-- Migration 025 : callback_url de la requête (/pay, /payout) gardé sur la transaction.
--
-- Le verdict peut être appliqué par le webhook agrégateur ou la vérification
-- manuelle (GET /payout/{ref}/check), hors de la requête d'origine : il faut
-- retrouver le webhook du client concerné pour lui renvoyer SA réponse.
-- Idempotent.

alter table transactions add column if not exists callback_url text;
