// Worker Cloudflare : arrêt nocturne de la machine Fly MobileWallet.
// 23:00 UTC (minuit Cameroun) : éteint (plus de redémarrage sur trafic).
// 03:00 UTC (4 h Cameroun)    : rallume en permanent.
// Secret FLY_API_TOKEN absent = ne fait RIEN (inactif tant qu'il n'est pas posé).
const API = 'https://api.machines.dev/v1';

async function fly(env, method, path, body) {
  const res = await fetch(`${API}/apps/${env.FLY_APP}${path}`, {
    method,
    headers: { Authorization: `Bearer ${env.FLY_API_TOKEN}`, 'Content-Type': 'application/json' },
    body: body ? JSON.stringify(body) : undefined,
  });
  const text = await res.text();
  if (!res.ok) throw new Error(`${method} ${path} -> ${res.status} ${text}`);
  return text ? JSON.parse(text) : null;
}

async function setAuto(env, m, autostart, autostop) {
  const config = { ...m.config, services: (m.config.services || []).map(s => ({ ...s, autostart, autostop })) };
  await fly(env, 'POST', `/machines/${m.id}`, { config, skip_launch: true });
}

async function run(env, action) {
  if (!env.FLY_API_TOKEN) {
    console.log(`[fly-night] ${action} ignoré : FLY_API_TOKEN absent`);
    return;
  }
  const machines = await fly(env, 'GET', '/machines');
  for (const m of machines) {
    if (action === 'off') {
      await setAuto(env, m, false, 'stop');
      await fly(env, 'POST', `/machines/${m.id}/stop`).catch(e => console.log(`[fly-night] stop ${m.id}: ${e.message}`));
    } else {
      await setAuto(env, m, true, 'off');
      await fly(env, 'POST', `/machines/${m.id}/start`).catch(e => console.log(`[fly-night] start ${m.id}: ${e.message}`));
    }
    console.log(`[fly-night] ${action} ✓ ${m.id}`);
  }
}

export default {
  async scheduled(event, env) {
    await run(env, event.cron === '0 23 * * *' ? 'off' : 'on');
  },
};
