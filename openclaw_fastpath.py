#!/usr/bin/env python3
"""
OpenClaw - Þrep 0: Deterministic Fast-Path Intent Router (0 tokens).

Hönnun:
  1. classify(text)      -> list[str]   Hrein regex-flokkun, engin I/O, < 1 ms.
  2. try_fast_path(text) -> FastPathResult | None
       Kallar á fasta verkfærafallið (read-only nema 'wake') og sniðmátar svarið
       fyrir ntfy. Skilar None ef fyrirspurnin á að fara í LLM-keðjuna.

Skýr mörk (boundary conditions) - fyrirspurn fer í LLM ef EITTHVAÐ af þessu á við:
  - orsaka-/röksemda-/leiðbeiningaspurning ("af hverju", "hvernig get ég", "útskýrðu"...)
  - texti byrjar á "Svar:" (Human Relay svar - má aldrei grípa)
  - lengri en MAX_WORDS orð / MAX_CHARS stafir
  - vakning (wake) án skýrs tækis, með spurningarmerki, neitun eða "get/ætti/hvort"
  - rafhlaða/hiti nefnd ásamt spjaldtölvu án þess að síminn sé nefndur (við höfum
    engin slík gögn um spjaldtölvuna - LLM/menn ráða við það)

Öryggi: notandatexti er ALDREI sendur áfram sem argument í verkfæri. Aðeins fastir
strengir (TABLET_TARGET) fara í tool_inspect_lan_host / tool_wake_device.
"""
from __future__ import annotations

import json
import os
import re
import time
import unicodedata
from dataclasses import dataclass, field
from typing import Callable, Optional

# --------------------------------------------------------------------------- #
# Stillingar
# --------------------------------------------------------------------------- #
USE_MARKDOWN = os.environ.get("OPENCLAW_MD", "0") == "1"   # **feitletrun** í ntfy?
SHOW_NETWORK_IDS = False      # IP/MAC í svörum? Slökkt því ntfy-rás getur verið opinber.
MAX_WORDS = 12
MAX_CHARS = 160
WAKE_COOLDOWN_S = 30
TABLET_TARGET = "tab4.local"
TEMP_WARN_C = 40.0

# --------------------------------------------------------------------------- #
# Textahreinsun: lágstafir + ASCII-fold svo "rafhlada"/"rafhlaða" séu eins
# --------------------------------------------------------------------------- #
_FOLD = str.maketrans({"á": "a", "é": "e", "í": "i", "ó": "o", "ú": "u", "ý": "y",
                       "ð": "d", "þ": "th", "æ": "ae", "ö": "o"})


def fold(s: str) -> str:
    s = unicodedata.normalize("NFC", s).lower().translate(_FOLD)
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


# Allar regexur vinna á FOLDUÐUM texta.
VETO = re.compile(
    r"\baf ?hverju\b|\bhvers vegna\b|\bhvi\b|\bhvad veldur\b|\bhvad er ad\b|\bhvad thydir\b"
    r"|\butskyr\w*|\bskyrdu\b|\bgreind\w*|\bsamanburd\w*|\bbera saman\b"
    r"|\baetti\b|\bmael\w*|\bradlegg\w*|\bhvad ef\b|\bgaeti\b|\bmyndi\b|\bhaegt\b"
    r"|\blaga\w*|\blagfaer\w*|\bvirkar ekki\b|\bvirka ekki\b|\bbilun\w*|\bbilad\w*"
    r"|\bvill(a|ur|u)\b|\berror\b|\btaemist\b|\btaemast\b|\btapast\b|\bfrystir\b"
    r"|\bkrass\w*|\bhrynur\b|\bget eg\b|\bgeturdu\b|\bgetur thu\b"
    r"|\bhvernig (a|get|gat|geri|gera|laga|lagfaera|virkar|virka|stilli|set|fae|kemst"
    r"|byrja|loka|opna|skipti|breyti|bua|forrita)\b"
)
TABLET = re.compile(r"\b(taba\w*|tab ?4|tab ?a|spjaldtolv\w*|galaxy tab)\b")
WAKE = re.compile(r"\b(vekj\w*|vektu|kveikj\w*|kveiktu|wake|wol)\b")
WAKE_BLOCK = re.compile(r"\b(ekki|aetti|get|geturdu|gaetir|mattu|viltu|hvort|myndi)\b|\ba eg\b")
KUMA_STRONG = re.compile(r"\b(kuma|vakt\w*|monitor\w*|uptime)\b")
KUMA_WEAK = re.compile(r"\b(nidri|nidur)\b")
UPDATES = re.compile(r"\b(uppfaersl\w*|oryggi\w*|cve\w*|patch\w*|updates?)\b|\bpakk\w* i bid\b")
PHONE_ID = re.compile(r"\b(sim(a|i|ann|inn|anum|ans)|r8q)\b")
PHONE_HW = re.compile(r"\b(rafhlad\w*|hledsl\w*|hledur|uppitim\w*|vinnsluminn\w*|diskpl\w*)\b")
TEMP_WORDS = re.compile(r"\b(hiti|hitinn|hitastig\w*|heitur|heitt|hversu heit\w*)\b")
GENERIC_STATUS = re.compile(r"\b(stada\w*|status)\b")


# --------------------------------------------------------------------------- #
# Flokkun (hrein, engin I/O)
# --------------------------------------------------------------------------- #
def classify(text: str) -> list[str]:
    """Skilar lista af intents (röð: device, tablet, kuma, updates) eða [] => LLM."""
    raw = (text or "").strip()
    if not raw or len(raw) > MAX_CHARS:
        return []
    if re.match(r"(?i)^\s*svar\s*:", raw):          # Human Relay svar
        return []
    f = fold(raw)
    if len(f.split()) > MAX_WORDS or VETO.search(f):
        return []

    tablet = bool(TABLET.search(f))

    if WAKE.search(f):                              # aðgerð með hliðaráhrifum: þröng skilyrði
        if not tablet or "?" in raw or WAKE_BLOCK.search(f):
            return []
        return ["wake"]

    phone_id = bool(PHONE_ID.search(f))
    phone_hw = bool(PHONE_HW.search(f))
    temp = bool(TEMP_WORDS.search(f))
    if tablet and (phone_hw or temp) and not phone_id:
        return []                                   # við eigum ekki rafhlöðu-/hitagögn úr Tab 4

    intents: list[str] = []
    if phone_id or phone_hw:
        intents.append("device")
    if tablet:
        intents.append("tablet")
    if KUMA_STRONG.search(f) or (KUMA_WEAK.search(f) and not tablet):
        intents.append("kuma")
    if UPDATES.search(f):
        intents.append("updates")
    if not intents and (GENERIC_STATUS.search(f) or temp):
        intents.append("device")
    return intents


# --------------------------------------------------------------------------- #
# Verkfæratenging (sprautað inn úr bridge-skriftunni)
# --------------------------------------------------------------------------- #
_TOOLS: dict[str, Callable[..., str]] = {}
_last_wake = 0.0


def bind_tools(*, status, inspect_lan, wake, updates, kuma) -> None:
    _TOOLS.update(status=status, inspect_lan=inspect_lan, wake=wake, updates=updates, kuma=kuma)


def _run(key: str, *args) -> str:
    fn = _TOOLS.get(key)
    if fn is None:
        raise RuntimeError(f"verkfæri ekki tengt: {key}")
    return fn(*args)


# --------------------------------------------------------------------------- #
# Umburðarlyndur JSON-lesari (lyklanöfn verkfæranna eru ekki negld niður)
# --------------------------------------------------------------------------- #
def _index(obj, prefix="", depth=0, out=None):
    out = [] if out is None else out
    if isinstance(obj, dict):
        for k, v in obj.items():
            path = f"{prefix}.{k}".lower() if prefix else str(k).lower()
            out.append((path, v))
            if depth < 3:
                _index(v, path, depth + 1, out)
    return out


def _find(idx, *aliases, scope=None, exact=False):
    for alias in aliases:
        for path, val in idx:
            if scope and not any(s in path for s in scope):
                continue
            last = path.rsplit(".", 1)[-1]
            if (alias == last) if exact else (alias in last):
                return val
    return None


def _num(v) -> Optional[float]:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        m = re.search(r"-?\d+(?:[.,]\d+)?", v)
        if m:
            return float(m.group().replace(",", "."))
    return None


def _is_up(v) -> bool:
    if isinstance(v, dict):
        v = v.get("active", v.get("state", v.get("status")))
    return v is True or v == 1 or str(v).strip().lower() in (
        "active", "up", "running", "ok", "1", "true", "yes", "online")


def _count(v) -> Optional[int]:
    if isinstance(v, (list, tuple)):
        return len(v)
    if isinstance(v, dict):
        n = _num(v.get("count"))
        return int(n) if n is not None else len(v)
    n = _num(v)
    return int(n) if n is not None else None


def _fmt_uptime(v) -> str:
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        secs = int(v)
        d, h, m = secs // 86400, secs % 86400 // 3600, secs % 3600 // 60
    else:
        s = str(v)
        got = {k: int(n) for n, k in re.findall(r"(\d+)\s*(week|day|hour|min)", s)}
        if not got:
            return s.replace("up ", "")[:30]
        d, h, m = got.get("week", 0) * 7 + got.get("day", 0), got.get("hour", 0), got.get("min", 0)
    parts = [(d, "d"), (h, "klst"), (m, "mín")]
    out = [f"{n} {u}" for n, u in parts if n][:2]
    return " ".join(out) or "< 1 mín"


def _generic(raw: str, limit: int = 8) -> str:
    try:
        flat = [(p, v) for p, v in _index(json.loads(raw)) if not isinstance(v, dict)]
    except Exception:
        return raw.strip()[:300]
    return "\n".join(f"• {p.rsplit('.', 1)[-1]}: {str(v)[:40]}" for p, v in flat[:limit])


def _b(s: str) -> str:
    return f"**{s}**" if USE_MARKDOWN else s


# --------------------------------------------------------------------------- #
# Sniðmát (farsímavæn, < ~8 línur)
# --------------------------------------------------------------------------- #
def _render_device(raw: str) -> str:
    idx = _index(json.loads(raw))
    lines = []

    cap = _num(_find(idx, "capacity", "percent", "level", "battery", "battery_percent"))
    state = _find(idx, "charging", "charge_state", "charge_status", "power_source", scope=("batt", "power", "charg"))
    if cap is not None:
        s = str(state).strip().lower() if state is not None else ""
        if "ac" in s or "hleðsl" in s or "charging" in s or s in ("true", "1"):
            extra = " · ⚡ hleður"
        elif "rafhl" in s or "discharging" in s or s in ("false", "0"):
            extra = " · 🔌 á rafhlöðu"
        elif "full" in s:
            extra = " · ✅ full"
        else:
            extra = f" · {s}" if s else ""
        lines.append(f"🔋 {_b(f'{cap:.0f}%')}{extra}")

    temp = _num(_find(idx, "temp", "temperature", "hiti", "battery_temp_c"))
    if temp is not None:
        temp = temp / 10 if temp > 100 else temp          # sysfs gefur tíundu hluta °C
        warn = " ⚠️ heitt" if temp >= TEMP_WARN_C else ""
        lines.append(f"🌡️ {_b(f'{temp:.1f} °C')}{warn}")

    up = _find(idx, "uptime")
    if up is not None:
        lines.append(f"⏱️ Uppitími: {_fmt_uptime(up)}")

    mem = _find(idx, "mem", "ram", "memory")
    if isinstance(mem, dict):
        used, total = _num(mem.get("used")), _num(mem.get("total"))
        if used is not None and total:
            lines.append(f"🧠 Minni: {used:g}/{total:g}")
    elif mem is not None:
        if isinstance(mem, str) and "Mem:" in mem:
            m = re.search(r"Mem:\s+(\S+)\s+(\S+)\s+\S+\s+\S+\s+\S+\s+(\S+)", mem)
            lines.append(f"🧠 Minni: {m.group(2)}/{m.group(1)} (laus: {m.group(3)})" if m else f"🧠 Minni: {mem.splitlines()[0][:30]}")
        else:
            lines.append(f"🧠 Minni: {str(mem)[:40]}")

    disk = _find(idx, "disk", "df", "root")
    if disk is not None:
        m = re.search(r"(\d+)\s*%", json.dumps(disk))
        lines.append(f"💾 Diskur: {m.group(1)}% notað" if m else f"💾 Diskur: {str(disk)[:30]}")

    svc = _find(idx, "services", "service")
    if isinstance(svc, dict) or isinstance(svc, list):
        pairs = (svc.items() if isinstance(svc, dict) else
                 [(x.get("name") or x.get("unit"), x.get("state", x.get("status", x.get("active"))))
                  for x in svc if isinstance(x, dict)])
        ok = [str(n) for n, s in pairs if _is_up(s)]
        bad = [str(n) for n, s in pairs if not _is_up(s)]
        if ok or bad:
            lines.append(f"✅ Þjónustur: {len(ok)}/{len(ok) + len(bad)}" if not bad
                         else f"⚠️ Þjónustur niðri: {', '.join(bad)}")

    if len(lines) < 2:
        return "📟 " + _b("r8q") + "\n" + _generic(raw)
    return "📟 " + _b("r8q") + "\n" + "\n".join(lines)


def _tablet_name() -> str:
    return "Tab 4"


def _render_tablet(raw: str) -> str:
    d = json.loads(raw)
    idx = _index(d)
    ok = _find(idx, "ping_ok", exact=True)
    if ok is None:
        return f"📱 {_b(_tablet_name())}\n" + _generic(raw)
    arp = _find(idx, "arp_matches", "arp")
    if _is_up(ok):
        line = f"✅ {_b('Uppi')} og svarar ping"
    elif arp:
        line = f"💤 {_b('Svarar ekki ping')} en sést á netinu (líklega í svefni)"
    else:
        line = f"❌ {_b('Ekki sýnilegur')} á heimanetinu"
    if SHOW_NETWORK_IDS:
        ips = _find(idx, "resolved_ips", "ping_target")
        if ips:
            line += f"\n🌐 {ips}"
    tail = "" if _is_up(ok) else "\n💡 Sendu „vekja taba“ til að reyna að vekja hann."
    return f"📱 {_b(_tablet_name())}\n{line}{tail}"


def _render_wake(raw: str) -> str:
    idx = _index(json.loads(raw))
    woke = _find(idx, "woke_up", exact=True)
    head = f"⏰ {_b('Vakningarpakki sendur')} á {_tablet_name()}"
    if woke is None:
        return head + "\n" + _generic(raw)
    if _is_up(woke):
        return head + "\n✅ Tækið svaraði - það er vaknað."
    return head + "\n⏳ Svarar ekki enn. Það getur tekið 10-30 sek; spurðu „er taba uppi?“ á eftir."


def _render_kuma(raw: str) -> str:
    idx = _index(json.loads(raw))
    mons = _find(idx, "monitors", "monitor")
    up = down = None
    names_down: list[str] = []
    pings: list[float] = []
    if isinstance(mons, list) and mons and isinstance(mons[0], dict):
        up = down = 0
        for m in mons:
            low = {str(k).lower(): v for k, v in m.items()}
            st = low.get("status", low.get("up", low.get("state")))
            name = low.get("name") or low.get("friendly_name") or low.get("monitor") or "?"
            if _is_up(st):
                up += 1
            else:
                down += 1
                names_down.append(str(name))
            p = _num(low.get("ping", low.get("latency", low.get("avg_ping"))))
            if p is not None:
                pings.append(p)
    else:
        u, dn = _num(_find(idx, "up", "up_count", exact=True)), _num(_find(idx, "down", "down_count", exact=True))
        up = int(u) if u is not None else None
        down = int(dn) if dn is not None else None
        avg = _num(_find(idx, "avg_ping", "ping"))
        if avg is not None:
            pings.append(avg)
    if up is None and down is None:
        return f"📡 {_b('Uptime Kuma')}\n" + _generic(raw)

    total = (up or 0) + (down or 0)
    lines = [f"📡 {_b('Vaktir')}: {up or 0}/{total} UP"]
    lines.append("✅ Allt uppi" if not down else f"⚠️ Niðri: {', '.join(names_down) or down}")
    if pings:
        lines.append(f"⏱️ Meðalsvartími: {sum(pings) / len(pings):.0f} ms")
    inc = _count(_find(idx, "incident", "incidents", "24h", "incidents_24h"))
    if inc is not None:
        lines.append(f"📈 Atvik síðustu 24 klst: {inc}")
    return "\n".join(lines)


def _render_updates(raw: str) -> str:
    idx = _index(json.loads(raw))
    pending = _count(_find(idx, "pending", "upgradable", "packages_pending", "packages", "total_count"))
    urgent = _count(_find(idx, "urgent", "cve", "critical", "security", "urgent_count"))
    tab = _find(idx, "tablet", "tab")
    lines = []
    if pending is not None or urgent is not None:
        row = f"🛡️ {_b('r8q')}: {pending if pending is not None else '?'} pakkar í bið"
        if urgent is not None:
            row += f" · {'🚨' if urgent else '✅'} {urgent} áríðandi CVE"
        lines.append(row)
    if tab is not None:
        if isinstance(tab, dict):
            sec = "✅ Öryggi í lagi" if tab.get("security_ok") else f"⚠️ Frávik: {", ".join(tab.get("issues", []))}"
            chks = tab.get("checks", {})
            bat = chks.get("tablet_battery_pct")
            extra = f" (🔋 {bat}%)" if bat is not None else ""
            lines.append(f"📱 Tab 4: {sec}{extra}")
        else:
            lines.append(f"📱 Tab 4: {str(tab)[:60]}")
    if not lines:
        return f"🛡️ {_b('Uppfærslur')}\n" + _generic(raw)
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Meðhöndlarar
# --------------------------------------------------------------------------- #
def _h_device(progress):  return _render_device(_run("status"))
def _h_tablet(progress):
    if progress:
        progress("⏳ Athuga Tab 4…")
    return _render_tablet(_run("inspect_lan", TABLET_TARGET))
def _h_kuma(progress):    return _render_kuma(_run("kuma"))
def _h_updates(progress): return _render_updates(_run("updates"))


def _h_wake(progress):
    global _last_wake
    now = time.monotonic()
    if now - _last_wake < WAKE_COOLDOWN_S:
        return "⏳ Vakningarpakki var nýlega sendur - bíddu aðeins og spurðu svo „er taba uppi?“"
    _last_wake = now
    if progress:
        progress("⏰ Sendi vakningarpakka á Tab 4…")
    return _render_wake(_run("wake", TABLET_TARGET))


_HANDLERS = {"device": _h_device, "tablet": _h_tablet, "kuma": _h_kuma,
             "updates": _h_updates, "wake": _h_wake}
_TITLES = {"device": "Staða r8q", "tablet": "Tab 4", "kuma": "Vaktir",
           "updates": "Uppfærslur", "wake": "Vakning"}
_TAGS = {"device": "battery", "tablet": "iphone", "kuma": "satellite",
         "updates": "shield", "wake": "alarm_clock"}


@dataclass
class FastPathResult:
    text: str
    title: str
    tags: list = field(default_factory=list)
    intents: list = field(default_factory=list)
    elapsed_ms: float = 0.0


def try_fast_path(text: str, progress: Optional[Callable[[str], None]] = None) -> Optional[FastPathResult]:
    """None => áfram í ask_openclaw() (LLM-keðjan)."""
    t0 = time.perf_counter()
    intents = classify(text)
    if not intents:
        return None
    parts = []
    for it in intents:
        try:
            parts.append(_HANDLERS[it](progress))
        except Exception as e:                       # verkfæri bilaði: segja það, ekki eyða tokens
            parts.append(f"⚠️ Gat ekki lesið „{_TITLES[it]}“: {type(e).__name__}: {str(e)[:80]}")
    return FastPathResult(
        text="\n\n".join(parts),
        title=_TITLES[intents[0]] if len(intents) == 1 else "Staða",
        tags=[_TAGS[i] for i in intents],
        intents=intents,
        elapsed_ms=(time.perf_counter() - t0) * 1000,
    )
