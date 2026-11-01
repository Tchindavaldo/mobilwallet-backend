# fly-night-worker (Cloudflare)

Arrêt nocturne de la machine Fly `mobilwallet-backend`, déclenché par Cloudflare
(à la minute près, contrairement aux crons GitHub qui prennent des heures de retard).

- `0 23 * * *` UTC (minuit Cameroun) : autostart désactivé + arrêt.
- `0 3 * * *` UTC (4 h Cameroun) : autostop `off` + démarrage (permanent).

Variable `FLY_APP = mobilwallet-backend`. Secret `FLY_API_TOKEN` (jeton Fly dédié) :
**absent = le Worker ne fait rien**. Le poser dans Cloudflare : Workers > fly-night-worker
> Settings > Variables and Secrets.
