---
name: km CLI setup
description: km binary location, auth method, and download URL for Komodo CLI on this Mac
type: reference
---

The `km` binary is NOT installed at `/opt/homebrew/bin/km` (the path referenced in the system prompt). It needs to be downloaded when needed.

**Download:** `curl -L https://github.com/moghtech/komodo/releases/download/v2.0.0/km-apple -o /tmp/km && chmod +x /tmp/km`

**Auth:** The `~/.config/komodo/komodo.cli.toml` config file does NOT work with km v2.0.0 (returns 403). Use env vars instead:

```bash
export KOMODO_API_KEY="K_hE4g2Ux21mLSr6loFjibhUEiK8tXyFeM3I7csL9O_K"
export KOMODO_API_SECRET="S_wHt30f9C1D8OWQJJicr7mkWDfBoT74gUt1u0zIb7_S"
export KOMODO_HOST="http://100.114.39.108:9120"
/tmp/km <command>
```

**Host:** Komodo core runs on werkstatt-1 at `http://100.114.39.108:9120` (direct, bypassing Caddy). The HTTPS host `komodo.cdit-dev.de` routes through Caddy on nebula-1 → werkstatt-1. Both work but direct HTTP is more reliable for the km CLI.

**Why:** Config file auth was returning 403 while env vars succeeded — likely a format incompatibility with km v2.0.0.
