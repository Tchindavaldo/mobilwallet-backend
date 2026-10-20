-- 029 : curl_templates -> digikuntz_curl_templates (règle CLAUDE.md : une table
-- propre à un agrégateur est préfixée par son nom ; le code lit
-- `<agrégateur>_curl_templates`). Seul DigiKUNTZ utilise le replay.
do $$
begin
    if to_regclass('public.curl_templates') is not null
       and to_regclass('public.digikuntz_curl_templates') is null then
        alter table curl_templates rename to digikuntz_curl_templates;
    end if;
end $$;
