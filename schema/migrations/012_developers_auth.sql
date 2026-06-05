-- 012_developers_auth.sql — authentification du compte developer (self-service).
--
-- Le dev s'inscrit/se connecte lui-même (email + mot de passe) pour gérer ses
-- apps et clés, sans passer par l'admin. On stocke le HASH du mot de passe
-- (bcrypt), jamais le mot de passe en clair. email_verified est posé pour un
-- futur flux de vérification email (inactif pour l'instant : compte actif direct).
--
-- Idempotent : exécutable sans risque même si déjà appliquée.

alter table developers add column if not exists password_hash  text;
alter table developers add column if not exists email_verified boolean not null default false;
