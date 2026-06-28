# AstrBotEX

AstrBotEX is a general embodied-runtime prototype. The first version is intentionally small:

- A platform-neutral Python core runtime.
- Typed plugin interfaces for vision, policy, skills, rules, and motion bridges.
- A static dashboard prototype styled after AstrBot's workbench layout.
- Mock plugins so the runtime can be exercised without hardware or YOLO.

## Layout

```text
astrbot_ex/
  core/         Runtime, world model, event bus, plugin registry.
  interfaces/   Stable contracts implemented by plugins.
  plugins/      Mock and built-in prototype plugins.
  profiles/     Mission/config profiles.
dashboard/      Static frontend prototype.
```

## Run Core Demo

```powershell
cd D:\Code\AstrBotEX
.\scripts\run_core_demo.ps1
```

## Run Local API

The first API server uses only the Python standard library so it can run on Windows
during development and later inside a Linux container on an embedded device.

```powershell
cd D:\Code\AstrBotEX
.\scripts\run_api_server.ps1
```

Cross-platform entry point:

```bash
python -m astrbot_ex.core.api_server --host 0.0.0.0 --port 8765 --tick-hz 5
```

Environment variables are also supported:

```text
ASTRBOTEX_HOST=0.0.0.0
ASTRBOTEX_PORT=8765
ASTRBOTEX_TICK_HZ=5
```

Current endpoints:

```text
GET  /api/status
GET  /api/events
POST /api/runtime/start
POST /api/runtime/stop
```

The same endpoints are also exposed under `/api/v1/ex/...` for the later stable
API namespace.

## Open Dashboard

```powershell
cd D:\Code\AstrBotEX
.\scripts\open_dashboard.ps1
```

You can also open `dashboard/index.html` directly in a browser.

This dashboard currently uses mock data. It is for UI structure and Core contract review before we wire a real API.
