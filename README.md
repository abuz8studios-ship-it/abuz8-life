# ABUZ8 Life

Electron shell over a live local probe daemon. No cloud, no account, loopback only.

- `shell/` — Electron app (tray, single instance, stale-daemon sweep, unbuffered spawn).
  `npm install && npm run dist` builds the Windows installer.
- `daemon/` — `lifed.py`, the probe daemon on `http://127.0.0.1:4310`
  (13 probes, gated action surface, triple-yes quarantine delete).
  `.\build.ps1` fuses the UI modules into `LIFE.html`; run `python -u lifed.py` to serve it.

Doctrine: wide surface, gates matched to blast radius. Probes never act on their own.
Delete needs triple-yes and lands in quarantine — nothing is ever unlinked.
