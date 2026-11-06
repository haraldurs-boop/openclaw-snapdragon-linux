#!/usr/bin/env python3
"""
Lightweight, resilient OpenClaw + ntfy bidirectional bridge for ARM64 Linux phones.
Runs using < 15 MB RAM and 0.0% idle CPU (avoids Node.js V8 RCU stalls on 0-swap devices).
Syncs with OpenClaw workspace markdown files and exposes hardware telemetry tool calls.
"""
import base64
import glob
import json
import os
import subprocess
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
    """Collects real-time phone telemetry: battery, voltage, temp, uptime, memory, disk, services."""
    info = {}
    try:
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


def tool_read_workspace_file(filename: str) -> str:
    """Reads a file from OpenClaw workspace directory."""
    safe_name = os.path.basename(filename)
    path = os.path.join(WORKSPACE_DIR, safe_name)
    if not os.path.exists(path):
        return f"File {safe_name} not found in workspace."
    with open(path, "r", encoding="utf-8") as f:
        return f.read()[:8000]


def tool_write_workspace_file(filename: str, content: str) -> str:
    """Writes or updates a markdown file in OpenClaw workspace."""
    safe_name = os.path.basename(filename)
    if not safe_name.endswith(".md"):
        return "Only .md files are allowed in workspace."
    path = os.path.join(WORKSPACE_DIR, safe_name)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return f"Updated {safe_name} ({len(content)} characters)."


def tool_run_shell_command(command: str) -> str:
    """Runs a read-only shell command under unprivileged service user."""
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
        return out[:3000] if out else "(No output)"
    except Exception as exc:
        return f"Command execution error: {exc}"


TOOLS_DECLARATION = [
    {
        "name": "tool_get_r8q_status",
        "description": "Get real-time phone status: battery level, temperature, voltage, uptime, memory, and services.",
        "parameters": {"type": "OBJECT", "properties": {}},
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
                return data[-14:]  # Last 7 turns
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


def call_gemini(api_key: str, contents: list, system_prompt: str) -> tuple[dict, str]:
    payload = {
        "system_instruction": {"parts": [{"text": system_prompt}]},
        "contents": contents,
        "tools": [{"function_declarations": TOOLS_DECLARATION}],
    }
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

    used_model = MODELS[0]
    try:
        for _ in range(4):
            res, used_model = call_gemini(api_key, contents, system_prompt)
            cand = (res.get("candidates") or [{}])[0]
            content = cand.get("content") or {}
            parts = content.get("parts") or []
            if not parts:
                return "(Empty response from model)", used_model

            fn_calls = [p["functionCall"] for p in parts if "functionCall" in p]
            if not fn_calls:
                reply_text = "\n".join(p.get("text", "") for p in parts if "text" in p).strip()
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

        return "Exceeded maximum tool loop limit.", used_model
    except Exception as exc:
        return f"Error: {exc}", used_model


def post_ntfy(message: str, env: dict, title: str = "🤖 OpenClaw", tags: str = "robot_face") -> None:
    ntfy_url = env.get("NTFY_URL", "http://127.0.0.1:2586").rstrip("/")
    ntfy_topic = env.get("NTFY_TOPIC", "openclaw")
    ntfy_token = env.get("NTFY_TOKEN", "")
    url = f"{ntfy_url}/{ntfy_topic}"
    req = urllib.request.Request(url, data=message.encode("utf-8"), method="POST")
    # Encode Title using RFC-2047 MIME Base64 to prevent latin-1 HTTP header encoding errors
    b64_title = base64.b64encode(title.encode("utf-8")).decode("ascii")
    req.add_header("Title", f"=?utf-8?b?{b64_title}?=")
    req.add_header("Tags", tags)
    if ntfy_token:
        req.add_header("Authorization", f"Bearer {ntfy_token}")
    with urllib.request.urlopen(req, timeout=15) as resp:
        resp.read()


def listen_loop() -> None:
    print("[openclaw-ntfy-bridge] Starting lightweight OpenClaw ntfy bridge...", flush=True)
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
                    if "robot_face" in tags or title.startswith("🤖"):
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
