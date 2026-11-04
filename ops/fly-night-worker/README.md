# fly-night-worker (Cloudflare)

Arrêt nocturne de la machine Fly `mobilwallet-backend`, déclenché par Cloudflare
(à la minute près, contrairement aux crons GitHub qui prennent des heures de retard).

- `0 3 * * *` UTC (4 h Cameroun) : autostop `off` + démarrage (permanent). Le
  démarrage attend 5 s puis réessaie toutes les 5 s pendant 1 min (Fly refuse le
  start, 412, pendant les ~2 s où il applique la nouvelle config).
- `0 3 * * *` UTC (4 h Cameroun) : autostop `off` + démarrage (permanent).

Variable `FLY_APP = mobilwallet-backend`. Secret `FLY_API_TOKEN` (jeton Fly dédié) :
**absent = le Worker ne fait rien**. Le poser dans Cloudflare : Workers > fly-night-worker
> Settings > Variables and Secrets.
