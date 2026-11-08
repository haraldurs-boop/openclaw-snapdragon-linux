# OpenClaw on Snapdragon Linux (`openclaw-snapdragon-linux`)

> **Battle-tested production guide, mitigations, and lightweight bridge for running autonomous AI agents 24/7 on mainline ARM64 smartphones (Qualcomm Snapdragon 865 / Samsung Galaxy S20 FE 5G) without thermal throttling, RCU scheduler stalls, or OOM crashes.**

---

### Attribution & Authorship Notice
> This project, architectural tuning, mitigations, and documentation were researched, engineered, and drafted by **Google DeepMind Antigravity**, pairing alongside the repository owner ([@haraldurs-boop](https://github.com/haraldurs-boop)). All code, kernel isolation parameters, and benchmark results were tested live on physical mobile hardware.

---

## 1. Motivation & Architecture

Modern smartphones running mainline Linux (e.g., Arch Linux ARM, postmarketOS, Debian/Mobian) make incredible edge servers:
- **Built-in UPS:** Internal battery provides hours of uninterrupted power.
- **Fast Storage:** High-speed UFS 3.1 storage.
- **Efficiency:** Big.LITTLE heterogeneous architecture (1x Prime Cortex-A77 + 3x Gold Cortex-A77 + 4x Silver Cortex-A55) idle at under 1.5 Watts.
- **Zero Fan Noise:** Completely silent 24/7 home server.

However, standard enterprise AI agent runtimes (like default OpenClaw Node.js gateways) assume high-powered x86_64 servers with gigabytes of swap and multi-fan cooling. On an embedded 0-swap smartphone kernel, default concurrency patterns trigger severe kernel lockups.

This repository provides the complete post-mortem, system tuning rules, and a lightweight native agent bridge dropping RAM consumption from **1,500 MB to 14 MB (a 100x reduction)**.

```
                                  Tailscale Funnel / Wi-Fi
                                             │
                                             ▼
                             ┌──────────────────────────────┐
                             │       ntfy Push Broker       │
                             │        (Port 2586)           │
                             └──────────────┬───────────────┘
                                            │ Local Loopback
                                            ▼
┌───────────────────────────────────────────────────────────────────────────┐
│ Samsung Galaxy S20 FE 5G (Qualcomm SM8250 - Mainline Linux 7.1.x)         │
│                                                                           │
│   Silver Cores (CPU 2-3) [Pinned via CPUAffinity=2 3]                     │
│   ┌───────────────────────────────────────────────────────────────────┐   │
│   │  openclaw-ntfy-bridge.py (Python 3.14 - 14.3 MB RAM, 0.0% Idle)   │   │
│   │                                                                   │   │
│   │  ├── OpenClaw Workspace Sync (SOUL.md, IDENTITY.md, MEMORY.md)    │   │
│   │  ├── Native Function Calling (Hardware telemetry, bash, files)    │   │
│   │  └── Cascading Resilient Model Chain:                             │   │
│   │        1. gemini-3.8-flash (Preview tier)                         │   │
│   │        2. gemini-3.6-flash (High availability)                    │   │
│   │        3. gemini-3.5-flash / gemini-2.5-flash (Emergency fallback)│   │
│   └─────────────────────────────────┬─────────────────────────────────┘   │
│                                     │ HTTPS                               │
└─────────────────────────────────────┼─────────────────────────────────────┘
                                      ▼
                        Google Gemini Developer API
```

---

## 2. Hard-Won Engineering Lessons

### A. The NPM Concurrency "Death Wedge" (RCU Preempt Stalls)
* **The Problem:** Running `npm install -g openclaw@latest` unpacks hundreds of packages and compiles native bindings across all 8 cores simultaneously. On an ARM64 big.LITTLE architecture with 0 B Swap, sudden multi-core memory allocation starves the kernel's `rcu_preempt` grace-period worker on CPU 1/3, triggering an instant kernel stall and freezing the device.
* **The Mitigation:** Pin `npm` strictly to two efficiency (Silver) cores with constrained concurrency:
  ```bash
  export UV_THREADPOOL_SIZE=1
  export NODE_OPTIONS="--max-old-space-size=512"
  taskset -c 2,3 nice -n 15 npm install -g openclaw@latest --maxsockets=2 --prefer-offline
  ```

### B. V8 Garbage Collection Thrashing vs. 24/7 Phone Thermals
* **The Problem:** The standard Node.js OpenClaw gateway loads over 360 ESM packages, sqlite vector modules, and background cron loops. On an embedded device running alongside existing services (AdGuard Home, local dashboards, Tailscale), RSS climbs to 1.2–1.8 GB. Constraining Node with `--max-old-space-size=512` induces continuous stop-the-world V8 GC loops (`main-thread stall: elapsedMs=21559`), eventually crashing the phone.
* **The Mitigation:** For mobile-first messaging (e.g. push via `ntfy`), use the provided [`openclaw-ntfy-bridge.py`](openclaw-ntfy-bridge.py):
  - Preserves standard OpenClaw workspace markdown conventions (`SOUL.md`, `IDENTITY.md`, `USER.md`, `MEMORY.md`).
  - Supports function calling (`get_r8q_status`, `read_workspace_file`, `write_workspace_file`, `run_shell_command`).
  - Memory consumption drops from **1,500 MB to 14.3 MB**.
  - CPU usage drops to **0.0%** at idle.

### C. Mainline Wi-Fi MAC Address Randomization
* **The Problem:** Qualcomm mobile Wi-Fi chips (`ath11k_pci` / QCA6390) frequently lack non-volatile EEPROM MAC storage under mainline device trees, randomly generating a MAC address on every boot. This exhausts DHCP pools and changes the phone's local IP address.
* **The Mitigation:** Lock the MAC address at the systemd-networkd level:
  ```ini
  # /etc/systemd/network/10-wifi.link
  [Match]
  Driver=ath11k_pci

  [Link]
  Name=wlan0
  MACAddress=00:03:7f:12:xx:xx
  ```

### D. 5-Layer Zero-Cost Pipeline (Deterministic Fast-Path + Cloud + Local Edge + Human Relay)
* **The Problem:** Free-tier API rate limits (HTTP 429 quota exhaustion) on cloud LLMs can stall agent workflows. Conversely, querying cloud LLMs for simple hardware status checks (battery %, uptime, monitor status) burns precious tokens and adds 1–3 seconds of unnecessary latency. Running continuous local LLMs on a phone heats up the battery and risks thermal throttling.
* **The Mitigation:** The bridge implements a multi-tier pipeline designed for high resilience, zero API cost, and sub-millisecond local execution:
  1. **Layer 0 (Deterministic Fast-Path Router - `openclaw_fastpath.py`):** Pure regex and deterministic command routing with **0 tokens and 0 LLM latency** (~10 microseconds classification, 1–8 ms file/SQLite read). Handles queries like status, battery, tablet reachability, Uptime Kuma monitors, and package updates directly on-device.
  2. **Layer 1 (Google Gemini Developer API):** Primary cloud reasoning engine with automatic fallback cascade across Gemini models (`gemini-3.8-flash` ➔ `gemini-3.6-flash` ➔ `gemini-3.5-flash` ➔ `gemini-2.5-flash`).
  3. **Layer 2 (Groq Cloud API):** Fast OpenAI-compatible cloud fallback (`openai/gpt-oss-120b`, `openai/gpt-oss-20b`, `qwen/qwen3.8-27b`) triggered seamlessly if Gemini returns HTTP 429/503.
  4. **Layer 3 (Cold-Run Local llama.cpp - `openclaw_localllm.py`):** On-device small model (`http://127.0.0.1:8081`) running exclusively during night hours (**23:30 – 07:10**) with thermal protection (rejects if battery >= 41°C) and tight window expiration guards.
  5. **Layer 4 (Human-in-the-Loop Prompt-Relay):** When all automated tiers are unavailable, OpenClaw pre-fetches system diagnostics, runs them through a strict two-way **Sanitization Engine** (stripping all real IPs, MACs, tailnet hostnames, and tokens), and posts a ready-to-copy Markdown bundle to `ntfy`. The user copies it to any free web LLM (ChatGPT, Claude, Grok) and replies via `ntfy` (`Svar: ...`). OpenClaw decodes the tokenized placeholders and persists the answer.

### E. Public Tailscale Funnel Action Buttons
* **The Problem:** Interactive notifications with HTTP Action buttons (`Actions: http, ...`) fail when constructed with loopback URLs (`http://127.0.0.1:2586`) because mobile clients cannot resolve `127.0.0.1` back to the server.
* **The Mitigation:** Action buttons are dynamically routed through the server's public Tailscale Funnel endpoint (`PUBLIC_NTFY_URL`), allowing one-tap system triage and update triggers from Pixel/mobile devices on home Wi-Fi and 4G/5G LTE alike.

---

## 3. Quick Start & Deployment

### 1. Requirements
- Mainline Linux on ARM64 (Arch Linux ARM, postmarketOS, Debian).
- Python 3.10+ (standard library only; zero external pip dependencies).
- A local or remote `ntfy` server.
- A Google Gemini API Key.

### 2. Service User Setup
```bash
useradd -m -s /bin/bash openclaw
chmod 700 /home/openclaw
mkdir -p /home/openclaw/.openclaw/workspace
```

### 3. Configure Environment
Create `/home/openclaw/.openclaw/.env`:
```ini
GEMINI_API_KEY=your_gemini_api_key_here
NTFY_URL=http://127.0.0.1:2586
NTFY_TOPIC=openclaw
NTFY_TOKEN=your_optional_ntfy_bearer_token
```

### 4. Deploy Systemd Service
Copy [`openclaw-ntfy-bridge.service`](openclaw-ntfy-bridge.service) to `/etc/systemd/system/openclaw-ntfy-bridge.service`:
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now openclaw-ntfy-bridge.service
```

Check resource usage:
```bash
systemctl status openclaw-ntfy-bridge.service
# Memory: ~14.3M
# CPU: 0.0% idle
```

---

## 4. Mobile Client Usage (Pixel / Android / iOS)

1. Install the official **ntfy** app from Google Play, F-Droid, or App Store.
2. Subscribe to your topic: `https://<your-server-or-tailscale-address>/openclaw`.
3. If protected, add your Access Token under topic settings.
4. Send a message: *"Hvernig líður símanum og hvað er rafhlaðan mikil?"* (How is the phone doing and what is battery level?).
5. OpenClaw executes `tool_get_r8q_status` directly on the phone and replies in seconds with battery %, temperature, voltage, and system uptime!

---

## 5. Security & Isolation

- **Non-root execution:** Runs strictly under the unprivileged `openclaw` user.
- **Sandboxed systemd flags:** `NoNewPrivileges=true`, `PrivateTmp=true`, `ProtectSystem=strict` (read-only OS paths except workspace).
- **Core isolation:** Pinned to Silver cores via `CPUAffinity=2 3` with `Nice=10` to guarantee phone communication and system daemons always take precedence.

---

## License
MIT
