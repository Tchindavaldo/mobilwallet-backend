-- 014_curl_templates_status.sql
-- Statut de fiabilité d'un template de replay, pour que le replay utilise
-- toujours le DERNIER template QUI A MARCHÉ et abandonne ceux qui échouent.
--
--   status:
--     'untested' : fraîchement déduit (mode browser), jamais encore rejoué.
--     'working'  : a déjà servi un replay qui a abouti (verdict terminal).
--     'failed'   : a échoué à l'usage (clé invalide/absente, chiffrement KO…)
--                  -> ne doit PLUS être réutilisé par le replay.
--
-- Le replay charge le dernier 'working', à défaut le dernier 'untested' ;
-- jamais un 'failed'. is_active reste l'index "version courante par agrégateur"
-- (au plus une), status qualifie sa fiabilité.

alter table curl_templates
    add column if not exists status text not null default 'untested';

-- Recherche rapide du meilleur template par agrégateur.
create index if not exists curl_templates_agg_status_idx
    on curl_templates (aggregator, status, created_at desc);
