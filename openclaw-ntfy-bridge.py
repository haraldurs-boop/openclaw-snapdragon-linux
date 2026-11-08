#!/usr/bin/env python3
"""
Lightweight, resilient OpenClaw + ntfy bidirectional bridge for ARM64 Linux phones.
Architecture & Loop Engine enhanced based on Gemini 4 Argon analysis:
- Guaranteed Final Synthesis: disables tools on final step to force answer summarization.
- Deduplication Guard: tracks seen tool calls to prevent repetitive execution loops.
- tool_inspect_lan_host: unified network inspector (DNS, ARP neighbours, ping).
- tool_wake_device: multi-stage wake-up engine (WoL Magic Packet + Wi-Fi DTIM Unicast burst).
"""
import base64
import glob
import json
import os
import re
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request

WORKSPACE_DIR = os.path.expanduser("~/.openclaw/workspace")
SESSION_FILE = os.path.expanduser("~/.openclaw/agents/main/sessions/ntfy_session.json")
ENV_FILE = os.path.expanduser("~/.openclaw/.env")

# Cascading fallback chain for resilience against demand spikes or quotas
MODELS = [
    "gemini-3.8-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-2.5-flash",
]

MAX_TOOL_ROUNDS = 4


def load_env() -> dict:
    env = dict(os.environ)
    if os.path.exists(ENV_FILE):
        with open(ENV_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip()
    return env


def load_workspace_prompt() -> str:
    parts = [
        "You are OpenClaw, a personal AI assistant running locally on an ARM64 Linux phone.",
        "Keep answers concise, actionable, and formatted cleanly for mobile messaging screens.",
        "\nTOOL USE & NETWORK RULES:",
        "- Use tools conservatively (maximum 3-4 calls per query).",
        "- For local network inspection, use `tool_inspect_lan_host`.",
        "- To attempt waking a sleeping device or tablet, use `tool_wake_device` (sends WoL + Wi-Fi DTIM wake bursts).",
        "- Explain results clearly: whether the target woke up and responded to ping, or remains unreachable (sleeping, powered off, or off-network).",
    ]
    for name in ("SOUL.md", "IDENTITY.md", "USER.md", "MEMORY.md"):
        p = os.path.join(WORKSPACE_DIR, name)
        if os.path.exists(p):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    content = f.read().strip()
                if content:
                    parts.append(f"\n--- {name} ---\n{content[:3000]}")
            except Exception:
                pass
    return "\n".join(parts)


def tool_get_r8q_status() -> str:
    """Collects real-time phone telemetry: battery, power source, temp, uptime, memory, disk, services."""
    info = {}
    try:
        charger_online = "0"
        for p in glob.glob("/sys/class/power_supply/*charger*/online"):
            charger_online = open(p).read().strip()
        info["power_source"] = "AC / Charger" if charger_online == "1" else "Battery (Discharging)"

        for p in glob.glob("/sys/class/power_supply/*battery*/capacity"):
            info["battery_percent"] = open(p).read().strip() + "%"
        for p in glob.glob("/sys/class/power_supply/*battery*/voltage_now"):
            v = int(open(p).read().strip()) / 1_000_000
            info["battery_voltage"] = f"{v:.2f} V"
        for p in glob.glob("/sys/class/power_supply/*battery*/current_now"):
            c = int(open(p).read().strip()) / 1000
            info["battery_current_mA"] = f"{c:.0f} mA"
        for p in glob.glob("/sys/class/power_supply/*battery*/temp"):
            t = int(open(p).read().strip()) / 10.0
            info["battery_temp_C"] = f"{t:.1f} °C"
    except Exception as e:
        info["battery_err"] = str(e)

    try:
        info["uptime"] = subprocess.check_output(["uptime", "-p"], text=True, timeout=5).strip()
        info["memory"] = subprocess.check_output(["free", "-h"], text=True, timeout=5).strip()
        info["disk"] = subprocess.check_output(["df", "-h", "/"], text=True, timeout=5).strip()
        svcs = ["adguardhome", "ntfy", "tailscaled", "openclaw-ntfy-bridge"]
        states = {}
        for s in svcs:
            r = subprocess.run(["systemctl", "is-active", s], capture_output=True, text=True, timeout=5)
            states[s] = r.stdout.strip()
        info["services"] = states
    except Exception as e:
        info["sys_err"] = str(e)
    return json.dumps(info, ensure_ascii=False, indent=2)


def tool_inspect_lan_host(target: str = "") -> str:
    """Unified LAN inspection: DNS/mDNS lookup, ARP neighbour cache, and ping."""
    target = (target or "").strip()
    res = {"target": target, "resolved_ips": [], "arp_matches": [], "ping_ok": False}

    if target:
        try:
            out = subprocess.run(["getent", "hosts", target], capture_output=True, text=True, timeout=3).stdout
            for line in out.splitlines():
                parts = line.split()
                if parts and parts[0] not in res["resolved_ips"]:
                    res["resolved_ips"].append(parts[0])
        except Exception:
            pass

    try:
        neigh_out = subprocess.run(["ip", "neigh", "show"], capture_output=True, text=True, timeout=3).stdout
        for line in neigh_out.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split()
            ip = parts[0]
            mac = parts[4] if len(parts) >= 5 and parts[3] == "lladdr" else "unknown"
            state = parts[-1]
            if target and (target in ip or target.lower() in line.lower()):
                res["arp_matches"].append({"ip": ip, "mac": mac, "state": state})
            elif not target:
                res["arp_matches"].append({"ip": ip, "mac": mac, "state": state})

        if not res["arp_matches"] and target:
            for line in neigh_out.splitlines()[:12]:
                parts = line.split()
                if parts and len(parts) >= 5:
                    res["arp_matches"].append({"ip": parts[0], "mac": parts[4], "state": parts[-1]})
    except Exception as e:
        res["arp_error"] = str(e)

    ping_target = res["resolved_ips"][0] if res["resolved_ips"] else (target if re.match(r"^\d+\.\d+\.\d+\.\d+$", target) else None)
    if ping_target:
        try:
            p = subprocess.run(["ping", "-c", "1", "-W", "2", ping_target], capture_output=True, text=True, timeout=4)
            res["ping_ok"] = (p.returncode == 0)
        except Exception:
            pass

    return json.dumps(res, ensure_ascii=False, indent=2)


def tool_wake_device(target: str = "taba.local") -> str:
    """Sends multi-stage wake-up packets (WoL Magic Packet + Wi-Fi DTIM Unicast burst)."""
    target = (target or "taba.local").strip()
    report = {"target": target, "actions": [], "woke_up": False, "target_ip": None, "mac": None}

    target_ip = None
    if re.match(r"^\d+\.\d+\.\d+\.\d+$", target):
        target_ip = target
    else:
        try:
            out = subprocess.run(["getent", "hosts", target], capture_output=True, text=True, timeout=3).stdout
            for line in out.splitlines():
                parts = line.split()
                if parts:
                    target_ip = parts[0]
                    break
        except Exception:
            pass

    report["target_ip"] = target_ip

    found_mac = None
    try:
        neigh_out = subprocess.run(["ip", "neigh", "show"], capture_output=True, text=True, timeout=3).stdout
        for line in neigh_out.splitlines():
            parts = line.split()
            if len(parts) >= 5 and parts[3] == "lladdr":
                if target_ip and parts[0] == target_ip:
                    found_mac = parts[4]
                    break
    except Exception:
        pass

    report["mac"] = found_mac

    # Stage A: Send WoL Magic Packet to broadcast
    try:
        mac_to_use = found_mac or "1a:af:e7:49:41:25"
        mac_bytes = bytes.fromhex(mac_to_use.replace(":", "").replace("-", ""))
        pkt = b"\xff" * 6 + mac_bytes * 16
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            s.sendto(pkt, ("192.168.68.255", 9))
            s.sendto(pkt, ("192.168.68.255", 7))
        report["actions"].append(f"WoL Magic Packet sent to broadcast for MAC {mac_to_use}.")
    except Exception as e:
        report["actions"].append(f"WoL error: {e}")

    # Stage B: Unicast Wi-Fi DTIM / Power-Save wake burst
    if target_ip:
        try:
            probe_ports = [22, 80, 5353, 9, 8080]
            for p in probe_ports:
                try:
                    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                        s.settimeout(0.2)
                        s.sendto(b"WAKEUP_PROBE", (target_ip, p))
                except Exception:
                    pass
            for p in [22, 80]:
                try:
                    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    s.settimeout(0.25)
                    s.connect_ex((target_ip, p))
                    s.close()
                except Exception:
                    pass
            report["actions"].append(f"Unicast Wi-Fi DTIM wake-burst sent to {target_ip} (ports {probe_ports}).")
        except Exception as e:
            report["actions"].append(f"DTIM burst error: {e}")

    # Stage C: Verification probe
    time.sleep(1.2)
    if target_ip:
        p = subprocess.run(["ping", "-c", "1", "-W", "2", target_ip], capture_output=True, text=True)
        report["woke_up"] = (p.returncode == 0)

    return json.dumps(report, ensure_ascii=False, indent=2)


def tool_read_workspace_file(filename: str) -> str:
    safe_name = os.path.basename(filename)
    path = os.path.join(WORKSPACE_DIR, safe_name)
    if not os.path.exists(path):
        return f"File {safe_name} not found in workspace."
    with open(path, "r", encoding="utf-8") as f:
        return f.read()[:8000]


def tool_write_workspace_file(filename: str, content: str) -> str:
    safe_name = os.path.basename(filename)
    if not safe_name.endswith(".md"):
        return "Only .md files are allowed in workspace."
    path = os.path.join(WORKSPACE_DIR, safe_name)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return f"Updated {safe_name} ({len(content)} characters)."


def tool_run_shell_command(command: str) -> str:
    try:
        res = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=15,
            cwd=WORKSPACE_DIR,
        )
        out = (res.stdout + res.stderr).strip()
        return f"[exit={res.returncode}]\n{out[:3000]}" if out else f"[exit={res.returncode}] (No output)"
    except Exception as exc:
        return f"[error] Command error: {exc}"


TOOLS_DECLARATION = [
    {
        "name": "tool_get_r8q_status",
        "description": "Get real-time phone status: battery level, power source, temperature, uptime, memory, disk, services.",
        "parameters": {"type": "OBJECT", "properties": {}},
    },
    {
        "name": "tool_inspect_lan_host",
        "description": "Inspect local network device or LAN status: DNS/mDNS query, ARP neighbours, and ping in one call.",
        "parameters": {
            "type": "OBJECT",
            "properties": {"target": {"type": "STRING", "description": "Hostname (.local), IP, or empty for active neighbours."}},
        },
    },
    {
        "name": "tool_wake_device",
        "description": "Attempt to wake a network device or tablet (e.g. taba.local) using WoL and Wi-Fi DTIM wake bursts.",
        "parameters": {
            "type": "OBJECT",
            "properties": {"target": {"type": "STRING", "description": "Hostname or IP address to wake."}},
        },
    },
    {
        "name": "tool_read_workspace_file",
        "description": "Read a file from workspace (SOUL.md, IDENTITY.md, USER.md, MEMORY.md).",
        "parameters": {
            "type": "OBJECT",
            "properties": {"filename": {"type": "STRING", "description": "e.g. MEMORY.md"}},
            "required": ["filename"],
        },
    },
    {
        "name": "tool_write_workspace_file",
        "description": "Save or update a markdown file in workspace.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "filename": {"type": "STRING", "description": "e.g. MEMORY.md"},
                "content": {"type": "STRING", "description": "Markdown content"},
            },
            "required": ["filename", "content"],
        },
    },
    {
        "name": "tool_run_shell_command",
        "description": "Execute a shell command as unprivileged user.",
        "parameters": {
            "type": "OBJECT",
            "properties": {"command": {"type": "STRING", "description": "Shell command"}},
            "required": ["command"],
        },
    },
]


def execute_tool_call(name: str, args: dict) -> str:
    if name == "tool_get_r8q_status":
        return tool_get_r8q_status()
    elif name == "tool_inspect_lan_host":
        return tool_inspect_lan_host(args.get("target", ""))
    elif name == "tool_wake_device":
        return tool_wake_device(args.get("target", "taba.local"))
    elif name == "tool_read_workspace_file":
        return tool_read_workspace_file(args.get("filename", ""))
    elif name == "tool_write_workspace_file":
        return tool_write_workspace_file(args.get("filename", ""), args.get("content", ""))
    elif name == "tool_run_shell_command":
        return tool_run_shell_command(args.get("command", ""))
    return f"Unknown tool: {name}"


def load_history() -> list:
    if os.path.exists(SESSION_FILE):
        try:
            with open(SESSION_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data[-14:]
        except Exception:
            pass
    return []


def save_history(history: list) -> None:
    os.makedirs(os.path.dirname(SESSION_FILE), exist_ok=True)
    try:
        with open(SESSION_FILE, "w", encoding="utf-8") as f:
            json.dump(history[-20:], f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def call_gemini(api_key: str, contents: list, system_prompt: str, enable_tools: bool = True) -> tuple[dict, str]:
    payload = {
        "system_instruction": {"parts": [{"text": system_prompt}]},
        "contents": contents,
    }
    if enable_tools:
        payload["tools"] = [{"function_declarations": TOOLS_DECLARATION}]
    else:
        payload["tool_config"] = {"function_calling_config": {"mode": "NONE"}}

    body = json.dumps(payload).encode("utf-8")
    last_err = None

    for model in MODELS:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8")), model
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")
            last_err = f"{model} returned HTTP {e.code}: {err_body[:200]}"
            print(f"[openclaw-bridge] {model} failed ({e.code}), falling back...", flush=True)
            time.sleep(0.5)
        except Exception as e:
            last_err = f"{model} error: {e}"
            print(f"[openclaw-bridge] {model} network error, falling back...", flush=True)
            time.sleep(0.5)

    raise RuntimeError(f"All Gemini models exhausted. Last error: {last_err}")


def ask_openclaw(prompt: str, env: dict) -> tuple[str, str]:
    text = prompt.strip()
    if text.lower() in ("/reset", "/clear"):
        save_history([])
        return "Conversation history cleared.", "system"

    api_key = env.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        return "GEMINI_API_KEY not found in environment.", "error"

    system_prompt = load_workspace_prompt()
    history = load_history()
    contents = list(history) + [{"role": "user", "parts": [{"text": text}]}]

    seen_calls = set()
    used_model = MODELS[0]

    try:
        for step in range(MAX_TOOL_ROUNDS + 1):
            is_final_step = (step == MAX_TOOL_ROUNDS)
            if is_final_step:
                contents.append({
                    "role": "user",
                    "parts": [{"text": "[SYSTEM DIRECTIVE: Maximum tool exploration reached. Synthesize a concise, helpful final answer for the user based on the evidence gathered above.]"}],
                })

            res, used_model = call_gemini(api_key, contents, system_prompt, enable_tools=not is_final_step)
            cand = (res.get("candidates") or [{}])[0]
            content = cand.get("content") or {}
            parts = content.get("parts") or []
            if not parts:
                return "(Empty response from model)", used_model

            fn_calls = [p["functionCall"] for p in parts if "functionCall" in p]
            if not fn_calls or is_final_step:
                reply_text = "\n".join(p.get("text", "") for p in parts if "text" in p).strip()
                if reply_text:
                    new_hist = list(history) + [
                        {"role": "user", "parts": [{"text": text}]},
                        {"role": "model", "parts": [{"text": reply_text}]},
                    ]
                    save_history(new_hist)
                return reply_text or "(Empty reply)", used_model

            contents.append({"role": "model", "parts": parts})
            fn_responses = []
            for fc in fn_calls:
                fn_name = fc.get("name", "")
                fn_args = fc.get("args") or {}
                call_sig = (fn_name, json.dumps(fn_args, sort_keys=True))
                if call_sig in seen_calls:
                    tool_out = "[System note: This tool call has already been executed in this turn. Do not call it again; summarize answer.]"
                else:
                    seen_calls.add(call_sig)
                    tool_out = execute_tool_call(fn_name, fn_args)
                fn_responses.append(
                    {
                        "functionResponse": {
                            "name": fn_name,
                            "response": {"result": tool_out},
                        }
                    }
                )
            contents.append({"role": "user", "parts": fn_responses})

        return "Completed turn.", used_model
    except Exception as exc:
        return f"Error: {exc}", used_model


def post_ntfy(message: str, env: dict, title: str = "🤖 OpenClaw", tags: str = "robot_face", priority: str = "default") -> None:
    ntfy_url = env.get("NTFY_URL", "http://127.0.0.1:2586").rstrip("/")
    ntfy_topic = env.get("NTFY_TOPIC", "openclaw")
    ntfy_token = env.get("NTFY_TOKEN", "")
    url = f"{ntfy_url}/{ntfy_topic}"
    req = urllib.request.Request(url, data=message.encode("utf-8"), method="POST")
    b64_title = base64.b64encode(title.encode("utf-8")).decode("ascii")
    req.add_header("Title", f"=?utf-8?b?{b64_title}?=")
    req.add_header("Tags", tags)
    if priority != "default":
        req.add_header("Priority", priority)
    if ntfy_token:
        req.add_header("Authorization", f"Bearer {ntfy_token}")
    with urllib.request.urlopen(req, timeout=15) as resp:
        resp.read()


def hardware_watchdog_loop() -> None:
    """Continuously monitors power state, thermals, and battery."""
    print("[openclaw-watchdog] Hardware watchdog loop started...", flush=True)
    last_charger_state = None
    last_temp_alert_time = 0

    while True:
        try:
            env = load_env()
            charger_online = None
            for p in glob.glob("/sys/class/power_supply/*charger*/online"):
                try:
                    charger_online = int(open(p).read().strip())
                except Exception:
                    pass

            cap = 0
            for p in glob.glob("/sys/class/power_supply/*battery*/capacity"):
                try:
                    cap = int(open(p).read().strip())
                except Exception:
                    pass

            if charger_online is not None:
                if last_charger_state is None:
                    last_charger_state = charger_online
                elif charger_online != last_charger_state:
                    if charger_online == 0:
                        msg = f"⚠️ Power cut detected (or charger unplugged)!\nPhone running on internal battery ({cap}% remaining)."
                        post_ntfy(msg, env, title="⚡ Power Outage / Battery", tags="warning,electric_plug", priority="high")
                    else:
                        msg = f"✅ Power restored!\nPhone charging again ({cap}% level)."
                        post_ntfy(msg, env, title="🔌 Power Restored", tags="white_check_mark,zap", priority="default")
                    last_charger_state = charger_online

            temp_c = 0
            for p in glob.glob("/sys/class/power_supply/*battery*/temp"):
                try:
                    temp_c = int(open(p).read().strip()) / 10.0
                except Exception:
                    pass
            if temp_c >= 45.0 and (time.time() - last_temp_alert_time > 1800):
                msg = f"🔥 Thermal alert on phone host!\nBattery temperature reached {temp_c:.1f}°C."
                post_ntfy(msg, env, title="🔥 Thermal Alert", tags="fire,warning", priority="high")
                last_temp_alert_time = time.time()

        except Exception as exc:
            print(f"[openclaw-watchdog] Watchdog error: {exc}", flush=True)

        time.sleep(10)


def listen_loop() -> None:
    print("[openclaw-ntfy-bridge] Starting OpenClaw ntfy bridge v4...", flush=True)
    t = threading.Thread(target=hardware_watchdog_loop, daemon=True)
    t.start()

    while True:
        env = load_env()
        ntfy_url = env.get("NTFY_URL", "http://127.0.0.1:2586").rstrip("/")
        ntfy_topic = env.get("NTFY_TOPIC", "openclaw")
        ntfy_token = env.get("NTFY_TOKEN", "")
        stream_url = f"{ntfy_url}/{ntfy_topic}/json"
        try:
            req = urllib.request.Request(stream_url)
            if ntfy_token:
                req.add_header("Authorization", f"Bearer {ntfy_token}")
            with urllib.request.urlopen(req, timeout=90) as resp:
                for raw_line in resp:
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue
                    data = json.loads(line)
                    if data.get("event") != "message":
                        continue
                    title = data.get("title") or ""
                    tags = data.get("tags") or []
                    msg = (data.get("message") or "").strip()
                    if "robot_face" in tags or title.startswith("🤖") or "warning" in tags or "white_check_mark" in tags:
                        continue
                    if not msg:
                        continue
                    print(f"[openclaw-ntfy-bridge] Query received: {msg[:80]}", flush=True)
                    reply, model = ask_openclaw(msg, load_env())
                    post_ntfy(reply, env, title=f"🤖 OpenClaw ({model})", tags="robot_face")
        except Exception as exc:
            print(f"[openclaw-ntfy-bridge] Reconnecting stream ({exc})", flush=True)
            time.sleep(2)


if __name__ == "__main__":
    listen_loop()
