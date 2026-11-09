#!/usr/bin/env python3
"""
OpenClaw - Þrep 3: On-Demand Local Llama Fallback (llama-server á 127.0.0.1:8081).

Hannað í samvinnu við Claude Sonnet:
  - Keyrir aðeins milli 23:30 og 07:10 (staðartími r8q símans).
  - On-Demand: ræsir llama-server.service gegnum systemd aðeins þegar þörf krefur (0 MB RAM í lausagangi).
  - Sjálfvirk slökkvun: slekkur á llama-server.service eftir 10 mínútna lausagang (idleStop).
  - Öryggisstöðvun: llama-night-stop.timer slekkur á þjónustunni óháð öllu kl. 07:10.
  - Aðgerðasía: ef fyrirspurn krefst aðgerða (breyta, vekja, uppfæra, keyra) er hafnað strax => Human Relay.
  - Enska til líkansins, engin tól (tools er ALDREI sent), max_tokens=250, timeout=120s.
  - Hvers kyns bilun => LocalLlamaUnavailable => kallandi fer í Human Relay (Þrep 4).
"""
from __future__ import annotations

import glob
import json
import re
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, time as dtime, timedelta
from typing import Callable, Optional

LLAMA_BASE_URL = "http://127.0.0.1:8081"
NIGHT_START = dtime(23, 30)
NIGHT_END = dtime(7, 10)
MIN_REMAINING_S = 90
THERMAL_LIMIT_C = 40.0
MAX_TOKENS = 250
TIMEOUT_S = 120
STARTUP_TIMEOUT_S = 60
IDLE_STOP_S = 300  # 5 mínútur (losar vinnsluminni fyrr)
MAX_QUERY_CHARS = 400
MODEL_LABEL = "qwen2.5-1.5b"

LOCAL_SYSTEM_PROMPT = (
    "You are a small offline backup assistant running on a phone; the main assistant is unavailable. "
    "Answer in English, in at most 3 short sentences. Plain text, no lists. The user may write in Icelandic. "
    "Still answer in English. If you are not sure you understood, say so and ask for a short rephrasing in English. "
    "You have no tools and no internet. Never claim to have checked or done anything. If you don't know, say 'I don't know'."
)

# Orð sem gefa til kynna að fyrirspurn krefjist aðgerðar/breytinga (sem 1.5B líkan má ALDREI gera)
ACTION_WORDS = (
    "vekja", "vaktu", "kveikja", "kveiktu", "slökkva", "slökktu",
    "endurræsa", "endurræstu", "endurræst", "uppfæra", "uppfærðu",
    "breyta", "breyttu", "skrifa", "skrifaðu", "eyða", "eyddu",
    "keyra", "keyrðu", "senda", "sendu", "bæta við",
    "install", "update", "reboot", "restart", "wake"
)


class LocalLlamaUnavailable(RuntimeError):
    """Kallandi á að falla yfir í Human Relay. .reason er stuttur kóði fyrir log."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _now() -> datetime:
    return datetime.now()


def is_night_window(now: Optional[datetime] = None) -> bool:
    t = (now or _now()).time()
    return t >= NIGHT_START or t < NIGHT_END


def seconds_left_in_window(now: Optional[datetime] = None) -> float:
    now = now or _now()
    if not is_night_window(now):
        return 0.0
    end_day = now.date() + timedelta(days=1) if now.time() >= NIGHT_START else now.date()
    return (datetime.combine(end_day, NIGHT_END) - now).total_seconds()


def _battery_temp_c() -> Optional[float]:
    for path in glob.glob("/sys/class/thermal/thermal_zone25/temp") + glob.glob("/sys/class/power_supply/*battery*/temp"):
        try:
            with open(path) as f:
                val = int(f.read().strip())
                return val / 1000.0 if val > 1000 else val / 10.0
        except (OSError, ValueError):
            continue
    return None


def _last_user_text(contents) -> str:
    """Sækir síðasta notendatexta úr Gemini-sniði (parts) eða OpenAI-sniði (content)."""
    for item in reversed(contents or []):
        if not isinstance(item, dict) or item.get("role", "user") != "user":
            continue
        if "parts" in item:
            txt = " ".join(p.get("text", "") for p in item["parts"] if isinstance(p, dict))
        else:
            c = item.get("content", "")
            txt = c if isinstance(c, str) else " ".join(
                p.get("text", "") for p in c if isinstance(p, dict))
        if txt.strip():
            return txt.strip()
    return ""


def _http_json(path: str, payload: Optional[dict] = None, timeout: float = 5) -> dict:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        LLAMA_BASE_URL + path, data=data,
        headers={"Content-Type": "application/json"},
        method="GET" if payload is None else "POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _is_server_healthy() -> bool:
    try:
        data = _http_json("/health", timeout=1.5)
        return data.get("status") == "ok"
    except Exception:
        return False


def _trim_to_sentence(text: str) -> str:
    m = re.search(r"^(.*[.!?])\s", text + " ", re.S)
    return m.group(1) if m else text


class LlamaLifecycleManager:
    """
    Stjórnar lífsferli llama-server.service:
      - Thread-safe gangsetning og bið.
      - Sjálfvirk stöðvun eftir 10 mínútna aðgerðaleysi.
    """

    def __init__(self, idle_seconds: int = IDLE_STOP_S):
        self.idle_seconds = idle_seconds
        self._lock = threading.Lock()
        self._idle_timer: Optional[threading.Timer] = None

    def _cancel_idle_timer(self) -> None:
        if self._idle_timer is not None:
            self._idle_timer.cancel()
            self._idle_timer = None

    def schedule_idle_stop(self) -> None:
        with self._lock:
            self._cancel_idle_timer()
            self._idle_timer = threading.Timer(self.idle_seconds, self.stop_service)
            self._idle_timer.daemon = True
            self._idle_timer.start()

    def stop_service(self) -> None:
        with self._lock:
            self._cancel_idle_timer()
            try:
                subprocess.run(["sudo", "-n", "/usr/bin/systemctl", "stop", "llama-server.service"],
                               check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception:
                pass

    def ensure_running(self, progress_cb: Optional[Callable[[str], None]] = None) -> None:
        """
        Gengur úr skugga um að llama-server sé í gangi og svari /health.
        Ef niðri: ræsir gegnum systemd og bíður þar til tilbúið (eða timeout).
        """
        with self._lock:
            self._cancel_idle_timer()
            if _is_server_healthy():
                return

            if progress_cb:
                try:
                    progress_cb("⏳ Ræsi staðbundið líkan (tekur ~7 sek)...")
                except Exception:
                    pass

            # Ræsa þjónustuna gegnum sudoers
            res = subprocess.run(
                ["sudo", "-n", "/usr/bin/systemctl", "start", "llama-server.service"],
                capture_output=True, text=True
            )
            if res.returncode != 0:
                raise LocalLlamaUnavailable(f"systemd_start_failed:{res.stderr.strip()[:60]}")

            # Bíða eftir að /health gefi grænt ljós
            t0 = time.monotonic()
            while time.monotonic() - t0 < STARTUP_TIMEOUT_S:
                time.sleep(0.5)
                if _is_server_healthy():
                    return

            # Ef ekki kominn upp innan tímamarka: stöðva og falla í Relay
            subprocess.run(["sudo", "-n", "/usr/bin/systemctl", "stop", "llama-server.service"],
                           check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            raise LocalLlamaUnavailable("startup_timeout")


_manager = LlamaLifecycleManager()


def call_local_llama(contents: list, system_prompt: str = "",
                     progress_cb: Optional[Callable[[str], None]] = None) -> tuple[dict, str]:
    """
    Skilar (result, provider_label).
      result = {"text", "finish_reason", "tool_calls": [], "usage", "latency_s"}
    `system_prompt` er haldið í undirskriftinni til samræmis við hin þrepin en er
    ekki notað (notum stuttan LOCAL_SYSTEM_PROMPT).
    Kastar LocalLlamaUnavailable í öllum bilunartilvikum => kallandi fer í Human Relay.
    """
    now = _now()
    if not is_night_window(now):
        raise LocalLlamaUnavailable("outside_night_window")
    if seconds_left_in_window(now) < MIN_REMAINING_S:
        raise LocalLlamaUnavailable("window_closing")

    temp = _battery_temp_c()
    if temp is not None and temp >= THERMAL_LIMIT_C:
        raise LocalLlamaUnavailable(f"thermal_guard_{temp:.1f}C")

    query = _last_user_text(contents)[:MAX_QUERY_CHARS]
    if not query:
        raise LocalLlamaUnavailable("empty_query")

    # Aðgerðasía: 1.5B líkanið á ALDREI að þykjast geta breytt eða framkvæmt aðgerðir
    q_lower = query.lower()
    if any(w in q_lower for w in ACTION_WORDS):
        raise LocalLlamaUnavailable("action_requires_human_or_tools")

    # Ræsa vélina on-demand ef þörf krefur
    _manager.ensure_running(progress_cb=progress_cb)

    payload = {
        "model": "local",
        "messages": [
            {"role": "system", "content": LOCAL_SYSTEM_PROMPT},
            {"role": "user", "content":
                f"User query (Icelandic): {query}\nProvide a concise 2-3 sentence answer."},
        ],
        "max_tokens": MAX_TOKENS,
        "temperature": 0.3,
        "top_p": 0.9,
        "repeat_penalty": 1.15,
        "cache_prompt": True,
        "stream": False,
    }
    t0 = time.monotonic()
    try:
        resp = _http_json("/v1/chat/completions", payload, timeout=TIMEOUT_S)
        choice = resp["choices"][0]
        text = (choice["message"]["content"] or "").strip()
    except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError,
            json.JSONDecodeError, KeyError, IndexError, TypeError) as e:
        _manager.schedule_idle_stop()
        raise LocalLlamaUnavailable(f"request_failed:{type(e).__name__}") from e

    # Endurstilla 10 mínútna sjálfvirkan slökkvara eftir hverja heppnaða fyrirspurn
    _manager.schedule_idle_stop()

    if not text:
        raise LocalLlamaUnavailable("empty_response")

    finish = choice.get("finish_reason")
    if finish == "length":
        text = _trim_to_sentence(text)

    result = {
        "text": text,
        "finish_reason": finish,
        "tool_calls": [],
        "usage": resp.get("usage", {}),
        "latency_s": round(time.monotonic() - t0, 1),
    }
    return result, f"local-llama:{MODEL_LABEL}"
