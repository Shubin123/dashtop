# dashtop

A tiny dashboard that lets a tablet (or any browser) watch a PC's live state
over the local network. The PC runs a small Python server; the tablet just
opens a web page — nothing to install on the tablet.

Shows: CPU (total, per-core, frequency), memory & swap, disk usage per volume,
disk I/O, network throughput, top processes, uptime, battery and temperatures
where available. History charts cover the last 5 or 15 minutes and update
every 2 seconds over Server-Sent Events.

Works on **Windows 10/11** and **Linux** (macOS too). Only dependency: `psutil`.
The page is fully self-contained — no internet access or CDN needed.

## Quick start — Windows 10

1. Install Python 3 from <https://www.python.org/downloads/> — tick
   **"Add python.exe to PATH"** during setup.
2. Copy this folder to the PC and double-click **`start.bat`**.
3. The first run, Windows Firewall will ask — click **Allow access**
   (Private networks is enough).
4. The console prints the address, e.g. `http://192.168.1.23:8010`.
   Open that on the tablet.
If the tablet is USB-connected and you want the dashboard to stay local-only, run the PC server with `--adb` and use:

```sh
adb reverse tcp:8010 tcp:8010
```

Then open `http://127.0.0.1:8010` on the tablet.
## Quick start — Linux

```sh
./start.sh
# or manually:
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python server.py
```

Open the printed `http://<pc-ip>:8010` on the tablet. If the tablet can't
reach it, allow the port through the firewall, e.g.
`sudo ufw allow 8010/tcp` (Ubuntu) or
`sudo firewall-cmd --add-port=8010/tcp --permanent && sudo firewall-cmd --reload` (Fedora).

## Options

```
python server.py --port 8010 --interval 2 --history 15
```

| Flag | Default | Meaning |
|---|---|---|
| `--port` | `8010` | HTTP port |
| `--host` | `0.0.0.0` | bind address (use `127.0.0.1` to keep it local-only) |
| `--adb` | `false` | bind to localhost and use `adb reverse` to let a connected tablet reach the page |
| `--interval` | `2` | seconds between samples |
| `--history` | `15` | minutes of chart history kept in memory |

## Endpoints

| Path | What |
|---|---|
| `/` | the dashboard page |
| `/api/summary` | JSON: static info + latest snapshot + history |
| `/api/stream` | Server-Sent Events, one snapshot per sample |

## Start automatically

**Windows 10** — Task Scheduler → Create Basic Task → trigger *When I log on* →
action *Start a program* → program `C:\path\to\dashtop\start.bat`. In the
task's properties, set *Start in* to the dashtop folder.

**Linux (systemd)** — save as `/etc/systemd/system/dashtop.service`:

```ini
[Unit]
Description=dashtop system dashboard
After=network.target

[Service]
ExecStart=/opt/dashtop/.venv/bin/python /opt/dashtop/server.py
Restart=on-failure
User=youruser

[Install]
WantedBy=multi-user.target
```

Then `sudo systemctl enable --now dashtop`.

## Tests

```sh
pip install -r requirements-dev.txt
python -m pytest tests/ -v
```

`tests/test_security.py` covers path traversal, method handling, and header
hygiene; `tests/test_performance.py` covers sampling cost, response latency,
payload size, concurrency, and history memory bounds.

## Security notes

dashtop is read-only — it never executes commands or accepts input that
changes the PC — but it **has no authentication**: anyone on your network can
view the stats (hostname, process names, IP). Run it on a trusted home/LAN
network only. Don't port-forward it to the internet as-is; if you need remote
access, put it behind a reverse proxy with auth (or a VPN like WireGuard/
Tailscale).
