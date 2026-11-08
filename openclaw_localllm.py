#!/usr/bin/env python3
"""
OpenClaw - Þrep 3: Cold-Run Local Llama Fallback (llama-server á 127.0.0.1:8081).

Reglur (úr verkefnislýsingu):
  - Aðeins milli 23:30 og 07:10 (staðartími símans).
  - Enska til líkansins, engin tól (tools er ALDREI sent), max_tokens=250, timeout=240s.
  - Hvers kyns bilun => LocalLlamaUnavailable => kallandi fer í Human Relay (Þrep 4).

Viðbótarvarnir (ódýrar og koma í veg fyrir bið í 4 mín. í von og vísa):
  - Gluggi að lokast: ekki hefja beiðni ef < MIN_REMAINING_S sek. eru eftir af glugganum.
  - Hitavörn: ef rafhlöðuhiti >= THERMAL_LIMIT_C er beðið um Human Relay í stað þess að hita símann.
  - /health forprófun (2 s): server niðri eða enn að hlaða líkan => strax í Relay.
"""
from __future__ import annotations

import glob
import json
import re
import socket
import time
import urllib.error
import urllib.request
from datetime import datetime, time as dtime, timedelta
from typing import Optional

LLAMA_BASE_URL = "http://127.0.0.1:8081"
NIGHT_START = dtime(23, 30)
NIGHT_END = dtime(7, 10)
MIN_REMAINING_S = 90
THERMAL_LIMIT_C = 41.0
MAX_TOKENS = 250
TIMEOUT_S = 240
MAX_QUERY_CHARS = 400
MODEL_LABEL = "qwen2.5-1.5b"

LOCAL_SYSTEM_PROMPT = (
    "You are OpenClaw's offline backup assistant running on a small phone. "
    "The user writes in Icelandic. Reply in English, at most 3 short sentences. "
    "You have no tools and no access to live device data: never invent numbers, "
    "statuses or file contents. If you cannot answer reliably, say so briefly."
)


class LocalLlamaUnavailable(RuntimeError):
    """Kallandi á að falla yfir í Human Relay. .reason er stuttur kóði fyrir log."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _now() -> datetime:                      # sér fall svo hægt sé að stubba í prófum
    return datetime.now()


def is_night_window(now: Optional[datetime] = None) -> bool:
    t = (now or _now()).time()
    return t >= NIGHT_START or t < NIGHT_END   # glugginn vefst yfir miðnætti


def seconds_left_in_window(now: Optional[datetime] = None) -> float:
    now = now or _now()
    if not is_night_window(now):
        return 0.0
    end_day = now.date() + timedelta(days=1) if now.time() >= NIGHT_START else now.date()
    return (datetime.combine(end_day, NIGHT_END) - now).total_seconds()


def _battery_temp_c() -> Optional[float]:
    for path in glob.glob("/sys/class/power_supply/*battery*/temp") + glob.glob("/sys/class/power_supply/*/temp"):
        try:
            with open(path) as f:
                return int(f.read().strip()) / 10.0
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
        if txt.strip():                        # sleppir functionResponse-hlutum (enginn texti)
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


def _trim_to_sentence(text: str) -> str:
    m = re.search(r"^(.*[.!?])\s", text + " ", re.S)
    return m.group(1) if m else text


def call_local_llama(contents: list, system_prompt: str = "") -> tuple[dict, str]:
    """
    Skilar (result, provider_label).
      result = {"text", "finish_reason", "tool_calls": [], "usage", "latency_s"}
    `system_prompt` er haldið í undirskriftinni til samræmis við hin þrepin en er
    VÍSVITANDI ekki notað: kerfisskipunin fyrir skýin er löng, íslensk og full af
    tóla-lýsingum og myndi sprengja samhengisgluggann hjá 1.5B líkaninu.
    Kastar LocalLlamaUnavailable í öllum bilunartilvikum.
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

    try:
        _http_json("/health", timeout=2)
    except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError,
            json.JSONDecodeError) as e:
        raise LocalLlamaUnavailable(f"server_down:{type(e).__name__}") from e

    payload = {                                  # ATH: engin "tools" / "tool_choice" lyklar
        "model": "local",
        "messages": [
            {"role": "system", "content": LOCAL_SYSTEM_PROMPT},
            {"role": "user", "content":
                f"User query (Icelandic): {query}\nProvide a concise 2-3 sentence answer."},
        ],
        "max_tokens": MAX_TOKENS,
        "temperature": 0.3,
        "top_p": 0.9,
        "repeat_penalty": 1.15,                  # llama.cpp viðbót: gegn endurtekningalykkjum
        "cache_prompt": True,                    # llama.cpp viðbót: endurnýtir kerfisskipun
        "stream": False,
    }
    t0 = time.monotonic()
    try:
        resp = _http_json("/v1/chat/completions", payload, timeout=TIMEOUT_S)
        choice = resp["choices"][0]
        text = (choice["message"]["content"] or "").strip()
    except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError,
            json.JSONDecodeError, KeyError, IndexError, TypeError) as e:
        raise LocalLlamaUnavailable(f"request_failed:{type(e).__name__}") from e
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
