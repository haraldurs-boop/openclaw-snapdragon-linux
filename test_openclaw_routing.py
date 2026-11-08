#!/usr/bin/env python3
"""Keyrsla: python3 test_openclaw_routing.py   (engin utanaðkomandi pakkar, engin raunverkfæri)"""
import json
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

import openclaw_fastpath as fp
import openclaw_localllm as ll

FAIL = 0


def check(name, cond, extra=""):
    global FAIL
    print(("PASS " if cond else "FAIL ") + name + (f"  {extra}" if extra and not cond else ""))
    FAIL += 0 if cond else 1


# ---------------------------------------------------------------- flokkun
FAST = {
    "staða": ["device"], "Staða?": ["device"], "status": ["device"],
    "hvernig líður símanum": ["device"], "hver er rafhlaðan": ["device"],
    "hvað er mikil hleðsla": ["device"], "hversu heitur er síminn": ["device"],
    "uppitími": ["device"], "hver er rafhlada": ["device"], "hiti": ["device"],
    "er taba uppi?": ["tablet"], "staða á spjaldtölvu": ["tablet"],
    "er kveikt á tab4?": ["tablet"], "ping taba": ["tablet"],
    "vekja taba": ["wake"], "kveikja á taba": ["wake"], "wake tab4": ["wake"],
    "vaktir": ["kuma"], "kuma": ["kuma"], "hvað er niðri?": ["kuma"], "uptime kuma": ["kuma"],
    "er taba niðri?": ["tablet"],
    "uppfærslur": ["updates"], "öryggi": ["updates"], "pakkar í bið": ["updates"],
    "rafhlaða símans og er taba uppi": ["device", "tablet"],
}
LLM = [
    "Af hverju tæmist rafhlaðan svona hratt?", "Hvernig get ég lagað ping villu?",
    "er hægt að vekja taba?", "ætti ég að vekja taba", "vekja taba?", "ekki vekja taba",
    "vekja", "hver er rafhlaðan á taba", "Svar: staða er góð", "segðu mér brandara",
    "hvað er klukkan", "útskýrðu uppfærslur fyrir mér", "hvernig virkar kuma",
    "get ég vakið taba", "ég vil að þú skoðir allar vaktir og berir saman við síðustu viku og sendir mér",
]
for q, exp in FAST.items():
    got = fp.classify(q)
    check(f"fast  {q!r} -> {exp}", got == exp, f"fékk {got}")
for q in LLM:
    got = fp.classify(q)
    check(f"llm   {q!r}", got == [], f"fékk {got}")

# ---------------------------------------------------------------- sniðmát + hraði
fp.bind_tools(
    status=lambda: json.dumps({"battery": {"capacity": 87, "status": "Discharging", "temp": 345},
        "uptime": "up 3 days, 4 hours, 5 minutes", "mem": {"used": 2.1, "total": 5.7},
        "disk": "/dev/sda 23% /", "services": {"adguardhome": "active", "tailscaled": "inactive"}}),
    inspect_lan=lambda t: json.dumps({"target": t, "ping_ok": False, "arp_matches": ["x"], "resolved_ips": ["1.2.3.4"]}),
    wake=lambda t: json.dumps({"target": t, "woke_up": True, "mac": "aa:bb"}),
    updates=lambda: json.dumps({"r8q": {"pending_packages": 3, "urgent_cves": 0}, "tablet": "uppfært"}),
    kuma=lambda: json.dumps({"monitors": [{"name": "AdGuard", "status": 1, "ping": 12},
                                          {"name": "Hub", "status": 0, "ping": 80}], "incidents_24h": 1}),
)
for q in ["staða", "er taba uppi", "vekja taba", "vaktir", "uppfærslur"]:
    fp._last_wake = 0.0
    r = fp.try_fast_path(q)
    check(f"render {q!r} < 50ms", r is not None and r.elapsed_ms < 50, f"{r and r.elapsed_ms:.1f}ms")
    print("     " + r.text.replace("\n", "\n     "))
r = fp.try_fast_path("staða")
check("ekkert MAC/IP lekið", "aa:bb" not in r.text and "1.2.3.4" not in r.text)
check("wake cooldown", (fp.try_fast_path("vekja taba"), fp.try_fast_path("vekja taba"))[1].text.startswith("⏳"))
check("None fyrir LLM-fyrirspurn", fp.try_fast_path("af hverju er síminn heitur") is None)
n = 2000
t = time.perf_counter()
for _ in range(n):
    fp.classify("hvernig líður símanum")
print(f"     classify: {(time.perf_counter() - t) / n * 1e6:.0f} µs/kall á þessari vél")

# ---------------------------------------------------------------- night window
D = lambda h, m: datetime(2026, 11, 8, h, m)
for (h, m), exp in {(23, 29): False, (23, 30): True, (0, 0): True, (7, 9): True, (7, 10): False, (14, 0): False}.items():
    check(f"night {h:02d}:{m:02d} == {exp}", ll.is_night_window(D(h, m)) == exp)
check("seconds_left 07:00 = 600", ll.seconds_left_in_window(D(7, 0)) == 600)
check("seconds_left 23:30 = 7h40m", ll.seconds_left_in_window(D(23, 30)) == 7 * 3600 + 40 * 60)

# ---------------------------------------------------------------- local llama gegn fölskum server
SEEN = {}


class Fake(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def _send(self, obj, code=200):
        b = json.dumps(obj).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)

    def do_GET(self):
        self._send({"status": "ok"})

    def do_POST(self):
        SEEN["body"] = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self._send({"choices": [{"message": {"content": "Battery is fine. Second sentence. Third cut off mid"},
                                 "finish_reason": "length"}], "usage": {"completion_tokens": 250}})


srv = HTTPServer(("127.0.0.1", 0), Fake)
threading.Thread(target=srv.serve_forever, daemon=True).start()
ll.LLAMA_BASE_URL = f"http://127.0.0.1:{srv.server_port}"
contents = [{"role": "user", "parts": [{"text": "hvernig er rafhlaðan að standa sig?"}]},
            {"role": "user", "parts": [{"functionResponse": {"name": "x"}}]}]

ll._now = lambda: D(14, 0)
try:
    ll.call_local_llama(contents, "x"); ok = False
except ll.LocalLlamaUnavailable as e:
    ok = e.reason == "outside_night_window"
check("dagur => Relay", ok)

ll._now = lambda: D(7, 9)
try:
    ll.call_local_llama(contents, "x"); ok = False
except ll.LocalLlamaUnavailable as e:
    ok = e.reason == "window_closing"
check("07:09 => window_closing", ok)

ll._now = lambda: D(2, 0)
ll._battery_temp_c = lambda: 45.0
try:
    ll.call_local_llama(contents, "x"); ok = False
except ll.LocalLlamaUnavailable as e:
    ok = e.reason.startswith("thermal_guard")
check("heitur sími => Relay", ok)

ll._battery_temp_c = lambda: 33.0
res, label = ll.call_local_llama(contents, "x")
body = SEEN["body"]
check("engin tools í payload", "tools" not in body and "tool_choice" not in body)
check("max_tokens == 250", body["max_tokens"] == 250)
check("enskt prompt + íslensk spurning", "Provide a concise 2-3 sentence answer." in body["messages"][1]["content"]
      and "rafhlaðan" in body["messages"][1]["content"])
check("trimmað að setningamörkum", res["text"] == "Battery is fine. Second sentence.", res["text"])
check("label", label.startswith("local-llama:"))

srv.shutdown(); srv.server_close()
try:
    ll.call_local_llama(contents, "x"); ok = False
except ll.LocalLlamaUnavailable as e:
    ok = e.reason.startswith("server_down")
check("server niðri => Relay", ok)

print("\nALLT Í LAGI" if not FAIL else f"\n{FAIL} PRÓF FÖLLUÐU")
raise SystemExit(1 if FAIL else 0)
