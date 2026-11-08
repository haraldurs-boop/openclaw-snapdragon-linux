#!/usr/bin/env python3
"""
Létt og örugg OpenClaw + ntfy tvístefnu-þjónusta fyrir Samsung S20 FE (r8q).
Hönnun og arkitektúr uppfært í samstarfi við Gemini 4 Argon:
- Tryggt lokasvar (Graceful Final Synthesis): slekkur á verkfærum á lokaskrefi.
- Tvítekningarvörn (seen_calls) kemur í veg fyrir endurtekin blindköll.
- tool_inspect_lan_host: skoðar stöðu tækja á heimanetinu (DNS, ARP, ping).
- tool_wake_device: fjölþrepa vöknunarkerfi (WoL + Wi-Fi DTIM Unicast wake-burst).
"""
import base64
import glob
import json
import os
import re
import socket
import sqlite3
import subprocess
import threading
import time
import urllib.error
import urllib.request
import sys
sys.path.insert(0, "/home/openclaw")
import openclaw_fastpath as fastpath
from openclaw_localllm import call_local_llama, is_night_window, LocalLlamaUnavailable

class TierUnavailable(Exception):
    """Kastað ef LLM þjónusta (Gemini eða Groq) lendir í 429/503 eða kvótabresti."""

WORKSPACE_DIR = "/home/openclaw/.openclaw/workspace"
SESSION_FILE = "/home/openclaw/.openclaw/agents/main/sessions/ntfy_session.json"
ENV_FILE = "/home/openclaw/.openclaw/.env"

MODELS = [
    "gemini-3.8-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-2.5-flash",
]

MAX_TOOL_ROUNDS = 4

PUBLIC_NTFY_URL = os.environ.get("PUBLIC_NTFY_URL", "https://your-node.tailnet.ts.net:10000")
PENDING_RELAY_FILE = "/tmp/openclaw_relay_state.json"


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
        "Þú ert OpenClaw, persónulegur gervigreindaraðstoðarmaður sem keyrir staðvært á Samsung Galaxy S20 FE 5G (r8q) "
        "með Mainline Linux 7.1.2 (Arch Linux ARM). Notandinn talar við þig úr Google Pixel símanum sínum í gegnum ntfy.",
        "Svaraðu alltaf á góðri íslensku nema beðið sé um annað. Hafðu svörin hnitmiðuð, skýr og hjálpleg fyrir farsímaskjá.",
        "\nREGLUR UM VERKFÆRAKÖLL OG HEIMANETIÐ:",
        "- Notaðu verkfæri sparlega (hámark 3-4 köll á fyrirspurn).",
        "- Ef spurt er um stöðu á spjaldtölvu eða heimaneti (t.d. tab4 / tab4.local / taba / Galaxy Tab 4), notaðu `tool_inspect_lan_host`.",
        "- Ef beðið er um að VEKJA tæki eða spjaldtölvu (tab4 / taba), notaðu `tool_wake_device` sem sendir bæði WoL Magic Packet og Wi-Fi DTIM Wake-burst.",
        "- Ef tækið vaknar (woke_up=True), staðfestu það gleðilega.",
        "- Ef tækið vaknar ekki (woke_up=False), útskýrðu faglega að vakningarpakkar hafi verið sendir en tækið svari ekki (líklega alveg slökkt á því, rafhlaðan tóm, eða utan þjónustusvæðis).",
        "\nÖRYGGIS- OG KERFISUPPFÆRSLUR (R8Q & GALAXY TAB 4):",
        "- Ef spurt er um öryggisstöðu, kerfisuppfærslur, pakka í bið eða stöðu spjaldtölvu: notaðu `tool_get_security_updates` til að skoða raunstöðuna úr /var/cache/r8q_update_status.json.",
        "- Útskýrðu niðurstöðuna skýrt: fjölda pakka í bið, hvort einhverjir séu brýnir (urgent) öryggispakkar, og hvort öryggi spjaldtölvunnar (Galaxy Tab 4) sé í lagi.",
        "- Ef notandi biður sérstaklega um að keyra eða hefja uppfærslu (eða staðfestir það í samtali), notaðu `tool_trigger_system_update(confirm=True)` sem ræsir örugga uppfærslu sem rótarnotandi utan minnishólfs þíns.",
        "- Kallaðu ALDREI á `tool_trigger_system_update(confirm=True)` nema notandi hafi beðið um eða samþykkt uppfærsluna.",
        "\nUPTIME KUMA OG NÆTURVAKT ANTIGRAVITY:",
        "- Ef spurt er um stöðu vefþjónusta, uppitíma, niðritíma eða Uptime Kuma (t.d. 'Hvað er niðri?', 'Er AdGuard uppi?'): notaðu `tool_get_uptime_kuma_status`.",
        "- Ef spurt er um næturvaktina, nýjustu næturskýrslu eða hvort Argon kvaðning bíði (t.d. 'Hvað gerði næturvaktin í nótt?'): notaðu `tool_get_nightly_report`.",
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
    """Sækir rauntímastöðu r8q símans: rafhlöðu, hleðslu, hita, uppitíma, vinnsluminni, disk og þjónustur."""
    info = {}
    try:
        charger_online = "0"
        for p in glob.glob("/sys/class/power_supply/*charger*/online"):
            charger_online = open(p).read().strip()
        info["power_source"] = "AC / Hleðslutæki" if charger_online == "1" else "🔋 Rafhlaða (Ekki í hleðslu)"

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
        svcs = ["adguardhome", "uptime-kuma", "infomentor-hub", "quota-web", "ntfy", "r8q-dashboard", "tailscaled", "openclaw-ntfy-bridge"]
        states = {}
        for s in svcs:
            r = subprocess.run(["systemctl", "is-active", s], capture_output=True, text=True, timeout=5)
            states[s] = r.stdout.strip()
        info["services"] = states
    except Exception as e:
        info["sys_err"] = str(e)
    return json.dumps(info, ensure_ascii=False, indent=2)


def tool_inspect_lan_host(target: str = "") -> str:
    """Kanna stöðu tækis eða heimanetsins: DNS/mDNS uppfletting, ARP nágrannar og ping."""
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
    if not ping_target and any(k in target.lower() for k in ["tab", "spjald"]):
        ping_target = "192.168.68.77"

    # Dýnamísk MAC-uppgötvun fyrir Galaxy Tab 4 (10:30:47:ea:d9:81) ef IP hefur breyst
    if any(k in target.lower() for k in ["tab", "spjald"]):
        for m in res.get("arp_matches", []):
            if m.get("mac", "").lower() == "10:30:47:ea:d9:81":
                arp_ip = m.get("ip")
                if arp_ip and arp_ip != ping_target:
                    res["discovered_mac_ip"] = arp_ip
                    ping_target = arp_ip
                break

    if ping_target:
        try:
            # 2 ping með 2 sekúndna bið til að gefa sofandi Wi-Fi viðmóti tíma til að vakna
            p = subprocess.run(["ping", "-c", "2", "-W", "2", ping_target], capture_output=True, text=True, timeout=5)
            res["ping_ok"] = (p.returncode == 0)
            res["ping_target"] = ping_target
        except Exception:
            pass

    return json.dumps(res, ensure_ascii=False, indent=2)


def tool_wake_device(target: str = "tab4.local") -> str:
    """Sendir fjölþrepa vakningarpakka á tæki (WoL Magic Packet + Wi-Fi DTIM Unicast burst)."""
    target = (target or "tab4.local").strip()
    report = {"target": target, "actions": [], "woke_up": False, "target_ip": None, "mac": None}

    # 1. Finna IP tölu
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

    if not target_ip and any(k in target.lower() for k in ["tab", "spjald"]):
        target_ip = "192.168.68.77"

    report["target_ip"] = target_ip

    # 2. Finna MAC tölu ef hún er í ARP töflunni eða uppgötva nýja IP miðað við fast MAC
    found_mac = None
    try:
        neigh_out = subprocess.run(["ip", "neigh", "show"], capture_output=True, text=True, timeout=3).stdout
        for line in neigh_out.splitlines():
            parts = line.split()
            if len(parts) >= 5 and parts[3] == "lladdr":
                if target_ip and parts[0] == target_ip:
                    found_mac = parts[4]
                if any(k in target.lower() for k in ["tab", "spjald"]) and parts[4].lower() == "10:30:47:ea:d9:81":
                    found_mac = parts[4]
                    if parts[0] != target_ip:
                        report["actions"].append(f"Spjaldtölva fannst á nýrri IP úr ARP: {parts[0]} (í stað {target_ip}).")
                        target_ip = parts[0]
                        report["target_ip"] = target_ip
                    break
    except Exception:
        pass

    if not found_mac and (target_ip == "192.168.68.77" or any(k in target.lower() for k in ["tab", "spjald"])):
        found_mac = "10:30:47:ea:d9:81"

    report["mac"] = found_mac

    # 3. Þrep A: Senda WoL Magic Packet
    try:
        # Ef við höfum ákveðið MAC, sendum á það. Annars sendum almennt broadcast.
        mac_to_use = found_mac or "10:30:47:ea:d9:81"
        mac_bytes = bytes.fromhex(mac_to_use.replace(":", "").replace("-", ""))
        pkt = b"\xff" * 6 + mac_bytes * 16
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            s.sendto(pkt, ("192.168.68.255", 9))
            s.sendto(pkt, ("192.168.68.255", 7))
        report["actions"].append(f"WoL Magic Packet sendur á útsendingarvistfang (broadcast) fyrir MAC {mac_to_use}.")
    except Exception as e:
        report["actions"].append(f"WoL villa: {e}")

    # 4. Þrep B: Unicast Wi-Fi DTIM / Power-Save Wake-Burst
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
            report["actions"].append(f"Unicast Wi-Fi DTIM/PSM wake-burst sendur á {target_ip} (port {probe_ports}).")
        except Exception as e:
            report["actions"].append(f"DTIM burst villa: {e}")

    # 5. Þrep C: Staðfesting - Bíða í 1.2 sek og pinga (2 tilraunir vegna DTIM)
    time.sleep(1.2)
    if target_ip:
        p = subprocess.run(["ping", "-c", "2", "-W", "2", target_ip], capture_output=True, text=True)
        report["woke_up"] = (p.returncode == 0)

    return json.dumps(report, ensure_ascii=False, indent=2)


def tool_read_workspace_file(filename: str) -> str:
    safe_name = os.path.basename(filename)
    path = os.path.join(WORKSPACE_DIR, safe_name)
    if not os.path.exists(path):
        return f"Skráin {safe_name} fannst ekki í workspace."
    with open(path, "r", encoding="utf-8") as f:
        return f.read()[:8000]


def tool_write_workspace_file(filename: str, content: str) -> str:
    safe_name = os.path.basename(filename)
    if not safe_name.endswith(".md"):
        return "Aðeins .md skrár eru leyfðar í workspace."
    path = os.path.join(WORKSPACE_DIR, safe_name)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return f"Uppfærði {safe_name} ({len(content)} stafir)."


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
        return f"[exit={res.returncode}]\n{out[:3000]}" if out else f"[exit={res.returncode}] (Engin úttak)"
    except Exception as exc:
        return f"[villa] Skipunarvilla: {exc}"


def tool_get_security_updates() -> str:
    """Les nýjustu stöðu öryggis- og kerfisuppfærslna fyrir R8Q og Galaxy Tab 4 úr /var/cache/r8q_update_status.json."""
    status_file = "/var/cache/r8q_update_status.json"
    if not os.path.exists(status_file):
        return json.dumps({
            "status": "unknown",
            "message": "Stöðuskrá finnst ekki (/var/cache/r8q_update_status.json). Keyrðu r8q-security-check.py fyrst."
        }, ensure_ascii=False)
    try:
        with open(status_file, "r", encoding="utf-8") as f:
            data = json.load(f)

        summary = {
            "date_str": data.get("date_str", "Óþekkt"),
            "urgency": data.get("urgency", "none"),
            "total_count": data.get("total_count", 0),
            "urgent_count": data.get("urgent_count", 0),
            "urgent_packages": data.get("urgent_packages", []),
            "packages_sample": data.get("packages", [])[:10],
            "tablet": {
                "security_ok": data.get("tablet", {}).get("security_ok", True),
                "issues": data.get("tablet", {}).get("issues", []),
                "checks": data.get("tablet", {}).get("checks", {}),
                "energy": data.get("tablet", {}).get("energy")
            }
        }
        return json.dumps(summary, ensure_ascii=False, indent=2)
    except Exception as exc:
        return json.dumps({"error": f"Villa við að lesa stöðuskrá: {exc}"}, ensure_ascii=False)


def tool_trigger_system_update(confirm: bool = False) -> str:
    """Ræsir örugga kerfisuppfærslu á R8Q gegnum infomentor-hub rótarvefþjóninn utan minnishólfs OpenClaw."""
    if not confirm:
        return (
            "AÐVÖRUN: Uppfærsla var EKKI ræst því staðfesting (confirm=True) vantaði. "
            "Spyrðu notandann fyrst hvort hann vilji hefja uppfærsluna áður en þú kallar á tólið með confirm=True."
        )

    status_file = "/var/cache/r8q_update_status.json"
    if not os.path.exists(status_file):
        return "Villa: Stöðuskrá finnst ekki (/var/cache/r8q_update_status.json). Get ekki sótt uppfærslu-auðkenni."

    try:
        with open(status_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        token = data.get("token")
        if not token:
            return "Villa: Ekkert öryggis-auðkenni (token) fannst í stöðuskrá."

        url = f"http://127.0.0.1:8080/api/update-confirm?token={token}&confirm=1"
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=20) as resp:
            content = resp.read().decode("utf-8", errors="replace")

        return f"Kerfisuppfærsla var virkjuð með árangri hjá kerfisþjóni (root)!\nSvar: {content[:400]}"
    except Exception as exc:
        return f"Villa við að virkja kerfisuppfærslu: {exc}"



def tool_get_uptime_kuma_status() -> str:
    """Sækir stöðu allra vakta í Uptime Kuma, svarhraða, og niðritímafrávik síðasta sólarhringinn."""
    db_path = "/opt/uptime-kuma/data/kuma.db"
    if not os.path.exists(db_path):
        return json.dumps({"error": f"Gagnagrunnur fannst ekki: {db_path}"}, ensure_ascii=False)

    conn = None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
    except sqlite3.OperationalError:
        try:
            conn = sqlite3.connect(f"file:{db_path}?immutable=1", uri=True, timeout=5)
        except Exception as exc:
            return json.dumps({"error": f"Gat ekki opnað gagnagrunn: {exc}"}, ensure_ascii=False)

    try:
        cur = conn.cursor()
        cur.execute("SELECT id, name, type, active, interval FROM monitor WHERE active = 1 ORDER BY id ASC")
        monitors = cur.fetchall()

        monitor_map = {}
        res = {"summary": {}, "monitors": [], "recent_downtimes": [], "active_incidents": []}
        up_count, down_count = 0, 0

        for mid, name, mtype, active, interval in monitors:
            monitor_map[mid] = name
            cur.execute("SELECT status, time, ping, msg FROM heartbeat WHERE monitor_id = ? ORDER BY id DESC LIMIT 1", (mid,))
            hb = cur.fetchone()
            st = "UP" if hb and hb[0] == 1 else ("DOWN" if hb and hb[0] == 0 else "PENDING")
            if st == "UP":
                up_count += 1
            elif st == "DOWN":
                down_count += 1

            res["monitors"].append({
                "id": mid,
                "name": name,
                "type": mtype,
                "status": st,
                "latency_ms": hb[2] if hb and hb[2] is not None else None,
                "last_checked": hb[1] if hb else None,
                "message": hb[3] if hb and hb[3] else ""
            })

        res["summary"] = {
            "total": len(monitors),
            "up": up_count,
            "down": down_count,
            "all_operational": down_count == 0
        }

        # 24h downtime events
        cur.execute("""
            SELECT monitor_id, time, msg 
            FROM heartbeat 
            WHERE important = 1 AND status = 0 AND time >= datetime('now', '-24 hours')
            ORDER BY id DESC LIMIT 10
        """)
        for dmid, dt, dmsg in cur.fetchall():
            res["recent_downtimes"].append({
                "monitor_id": dmid,
                "monitor_name": monitor_map.get(dmid, f"Monitor #{dmid}"),
                "time": dt,
                "message": dmsg or ""
            })

        # Active incidents
        cur.execute("SELECT id, title, content, style, created_date FROM incident WHERE active = 1")
        for inc in cur.fetchall():
            res["active_incidents"].append({
                "id": inc[0],
                "title": inc[1],
                "content": inc[2],
                "style": inc[3],
                "created_date": inc[4]
            })

        conn.close()
        return json.dumps(res, ensure_ascii=False, indent=2)
    except Exception as exc:
        if conn:
            conn.close()
        return json.dumps({"error": f"Villa við lestur á Kuma gögnum: {exc}"}, ensure_ascii=False)


def tool_get_nightly_report() -> str:
    """Les nýjustu skýrslu Antigravity næturvaktarinnar og athugar hvort virk Argon-kvaðning sé í biðröð."""
    report_path = "/opt/antigravity-nightly/reports/latest.md"
    spool_dir = "/var/spool/antigravity-alerts"
    
    res = {
        "report_found": False,
        "report_date": "Óþekkt",
        "content": "",
        "pending_argon_escalations": []
    }

    if os.path.exists(report_path):
        try:
            res["report_found"] = True
            with open(report_path, "r", encoding="utf-8") as f:
                res["content"] = f.read()[:6000]
            m = re.search(r"\b(202\d-\d{2}-\d{2})\b", res["content"])
            if m:
                res["report_date"] = m.group(1)
        except Exception as exc:
            res["content_error"] = str(exc)
    else:
        res["content"] = "Engin næturskýrsla fannst í /opt/antigravity-nightly/reports/latest.md."

    if os.path.exists(spool_dir):
        try:
            for p in glob.glob(os.path.join(spool_dir, "pending_*.json")):
                with open(p, "r", encoding="utf-8") as f:
                    res["pending_argon_escalations"].append(json.load(f))
        except Exception as exc:
            res["spool_error"] = str(exc)

    return json.dumps(res, ensure_ascii=False, indent=2)


TOOLS_DECLARATION = [
    {
        "name": "tool_get_uptime_kuma_status",
        "description": "Sækir stöðu allra vakta í Uptime Kuma (AdGuard, ntfy, SSH, InfoMentor, o.fl.), svarhraða og frávik síðasta sólarhringinn.",
        "parameters": {"type": "OBJECT", "properties": {}},
    },
    {
        "name": "tool_get_nightly_report",
        "description": "Sækir nýjustu skýrslu Antigravity næturvaktarinnar (kerfisheilsa, rafhlaða, villur) og athugar hvort kvaðning á Argon sé í biðröð.",
        "parameters": {"type": "OBJECT", "properties": {}},
    },

    {
        "name": "tool_get_r8q_status",
        "description": "Sækir rauntímastöðu símans: rafhlöðu, hleðslu/straumgjafa, hita, uppitíma og þjónustur.",
        "parameters": {"type": "OBJECT", "properties": {}},
    },
    {
        "name": "tool_get_security_updates",
        "description": "Sækir stöðu öryggis- og kerfisuppfærslna á R8Q og Galaxy Tab 4 úr /var/cache/r8q_update_status.json.",
        "parameters": {"type": "OBJECT", "properties": {}},
    },
    {
        "name": "tool_trigger_system_update",
        "description": "Ræsir uppsetningu kerfisuppfærslna á R8Q í bakgrunni sem root utan minnishólfs OpenClaw. Krefst confirm=True.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "confirm": {
                    "type": "BOOLEAN",
                    "description": "Verður að vera True til að framkvæma uppfærsluna eftir samþykki notanda.",
                }
            },
            "required": ["confirm"],
        },
    },
    {
        "name": "tool_inspect_lan_host",
        "description": "Kanna tæki eða stöðu heimanetsins: getent/mDNS uppfletting, ARP nágrannar og ping.",
        "parameters": {
            "type": "OBJECT",
            "properties": {"target": {"type": "STRING", "description": "Lén, hýsilnafn (t.d. tab4, tab4.local eða taba) eða IP-tala."}},
        },
    },
    {
        "name": "tool_wake_device",
        "description": "Reyna að vekja tæki eða spjaldtölvu (t.d. tab4 / tab4.local / taba) úr dvala með WoL og Wi-Fi wake-burst.",
        "parameters": {
            "type": "OBJECT",
            "properties": {"target": {"type": "STRING", "description": "Hýsilnafn t.d. tab4.local, taba.local eða IP tala."}},
        },
    },
    {
        "name": "tool_read_workspace_file",
        "description": "Les skrá úr OpenClaw workspace möppunni (SOUL.md, IDENTITY.md, USER.md, MEMORY.md).",
        "parameters": {
            "type": "OBJECT",
            "properties": {"filename": {"type": "STRING", "description": "T.d. MEMORY.md"}},
            "required": ["filename"],
        },
    },
    {
        "name": "tool_write_workspace_file",
        "description": "Vistar eða uppfærir markdown-skrá í OpenClaw workspace.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "filename": {"type": "STRING", "description": "T.d. MEMORY.md"},
                "content": {"type": "STRING", "description": "Innihald skrárinnar"},
            },
            "required": ["filename", "content"],
        },
    },
    {
        "name": "tool_run_shell_command",
        "description": "Keyrir örugga Linux skipun sem notandinn openclaw.",
        "parameters": {
            "type": "OBJECT",
            "properties": {"command": {"type": "STRING", "description": "Skeljarskipun"}},
            "required": ["command"],
        },
    },
]

TOOL_DECLARATIONS = TOOLS_DECLARATION


def execute_tool_call(name: str, args: dict) -> str:
    if name == "tool_get_r8q_status":
        return tool_get_r8q_status()
    elif name == "tool_get_security_updates":
        return tool_get_security_updates()
    elif name == "tool_trigger_system_update":
        confirm_val = bool(args.get("confirm", False))
        return tool_trigger_system_update(confirm=confirm_val)
    elif name == "tool_inspect_lan_host":
        return tool_inspect_lan_host(args.get("target", ""))
    elif name == "tool_wake_device":
        return tool_wake_device(args.get("target", "tab4.local"))
    elif name == "tool_read_workspace_file":
        return tool_read_workspace_file(args.get("filename", ""))
    elif name == "tool_write_workspace_file":
        return tool_write_workspace_file(args.get("filename", ""), args.get("content", ""))
    elif name == "tool_run_shell_command":
        return tool_run_shell_command(args.get("command", ""))
    elif name == "tool_get_uptime_kuma_status":
        return tool_get_uptime_kuma_status()
    elif name == "tool_get_nightly_report":
        return tool_get_nightly_report()
    return f"Óþekkt verkfæri: {name}"


execute_tool = execute_tool_call

fastpath.bind_tools(
    status=tool_get_r8q_status,
    inspect_lan=tool_inspect_lan_host,
    wake=tool_wake_device,
    updates=tool_get_security_updates,
    kuma=tool_get_uptime_kuma_status,
)


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


def call_gemini(api_key: str, contents: list, system_prompt: str, enable_tools: bool = True, max_output_tokens: int = 0) -> tuple[dict, str]:
    payload = {
        "system_instruction": {"parts": [{"text": system_prompt}]},
        "contents": contents,
    }
    if max_output_tokens > 0:
        payload["generationConfig"] = {
            "maxOutputTokens": max_output_tokens,
            "thinkingConfig": {"thinkingBudget": 0}
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
            last_err = f"{model} skilaði HTTP {e.code}: {err_body[:200]}"
            print(f"[openclaw-bridge] {model} brást ({e.code}), reyni næsta líkan...", flush=True)
            time.sleep(0.5)
        except Exception as e:
            last_err = f"{model} villa: {e}"
            print(f"[openclaw-bridge] {model} nettengingarvilla, reyni næsta líkan...", flush=True)
            time.sleep(0.5)

    raise TierUnavailable(f"Öll Gemini líkön mistókust. Síðasta villa: {last_err}")

GROQ_MODELS = [
    "openai/gpt-oss-120b",
    "openai/gpt-oss-20b",
    "qwen/qwen3.8-27b",
]


def call_groq(api_key: str, contents: list, system_prompt: str, enable_tools: bool = True) -> tuple[dict, str]:
    """Kallar á Groq (GPT-OSS 120B / 20B / Qwen 27B) og skilar svari á nákvæmlega sama innra sniði og call_gemini."""
    messages = [{"role": "system", "content": system_prompt}]
    for turn in contents:
        role = "assistant" if turn["role"] == "model" else "user"
        text_parts = [p["text"] for p in turn.get("parts", []) if "text" in p]
        fn_res = [p["functionResponse"] for p in turn.get("parts", []) if "functionResponse" in p]
        if text_parts:
            messages.append({"role": role, "content": "\n".join(text_parts)})
        elif fn_res:
            messages.append({"role": "user", "content": "Niðurstaða verkfæris:\n" + json.dumps(fn_res, ensure_ascii=False)})

    openai_tools = None
    if enable_tools:
        openai_tools = []
        for t in TOOLS_DECLARATION:
            params = json.loads(json.dumps(t["parameters"]).lower())
            openai_tools.append({
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t["description"],
                    "parameters": params,
                },
            })

    last_err = None
    for model in GROQ_MODELS:
        payload = {"model": model, "messages": messages, "temperature": 0.2}
        if openai_tools:
            payload["tools"] = openai_tools

        req = urllib.request.Request(
            "https://api.groq.com/openai/v1/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
                "User-Agent": "openclaw/1.0",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=25) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            msg = data["choices"][0]["message"]
            parts = []
            if msg.get("content"):
                parts.append({"text": msg["content"]})
            for tc in msg.get("tool_calls") or []:
                fn = tc["function"]
                args = json.loads(fn.get("arguments") or "{}") if isinstance(fn.get("arguments"), str) else (fn.get("arguments") or {})
                parts.append({"functionCall": {"name": fn["name"], "args": args}})

            return {"candidates": [{"content": {"parts": parts}}]}, f"groq/{model}"
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")
            last_err = f"{model} skilaði HTTP {e.code}: {err_body[:200]}"
            print(f"[openclaw-bridge] Groq {model} brást ({e.code}), reyni næsta Groq líkan...", flush=True)
            time.sleep(0.5)
        except Exception as e:
            last_err = f"{model} villa: {e}"
            print(f"[openclaw-bridge] Groq {model} tengingarvilla, reyni næsta Groq líkan...", flush=True)
            time.sleep(0.5)

    raise TierUnavailable(f"Öll Groq líkön mistókust. Síðasta villa: {last_err}")


def sanitize_for_external_llm(text: str) -> tuple[str, dict]:
    """Hreinsar út allar viðkvæmar upplýsingar (IP-tölur, MAC, lén, API-lykla, nöfn) og býr til táknkort."""
    mapping = {}

    def mask_secret(m):
        val = m.group(0)
        token = "<SECRET_TOKEN>"
        mapping[token] = val
        return token

    # Gemini API lyklar
    text = re.sub(r"\b(?:AIza[a-zA-Z0-9_\-]{35}|AQ\.[a-zA-Z0-9_\-\.]{20,})\b", mask_secret, text)
    # Groq API lyklar
    text = re.sub(r"\bgsk_[a-zA-Z0-9]{20,}\b", mask_secret, text)
    # ntfy lyklar
    text = re.sub(r"\btk_[a-zA-Z0-9_-]{20,}\b", mask_secret, text)
    # Almenn hex öryggistákn (t.d. update token)
    text = re.sub(r"\b[0-9a-fA-F]{32,64}\b", mask_secret, text)

    # Tailscale lén
    def mask_tailnet(m):
        val = m.group(0)
        token = "<TAILNET_DOMAIN>"
        mapping[token] = val
        return token
    text = re.sub(r"\b[a-zA-Z0-9_\-]+\.tail[a-zA-Z0-9]+\.ts\.net(?::\d+)?\b", mask_tailnet, text)

    # Þekkt heimilistæki og hýsingar
    known_replacements = [
        ("192.168.68.87", "<SERVER_IP>"),
        ("192.168.68.77", "<TABLET_HOST>"),
        ("192.168.68.75", "<TABLET_HOST>"),
        ("192.168.68.1", "<ROUTER_IP>"),
        ("tab4.local", "<TABLET_HOST>"),
        ("tab4", "<TABLET_HOST>"),
        ("taba.local", "<TABLET_HOST>"),
        ("taba", "<TABLET_HOST>"),
        ("10:30:47:ea:d9:81", "<TABLET_MAC>"),
        ("1a:af:e7:49:41:25", "<TABLET_MAC>"),
    ]
    for orig, rep in known_replacements:
        if orig.lower() in text.lower():
            mapping[rep] = orig
            text = re.sub(re.escape(orig), rep, text, flags=re.IGNORECASE)

    # MAC vistföng
    def mask_mac(m):
        val = m.group(0)
        token = "<MAC_REDACTED>"
        mapping[token] = val
        return token
    text = re.sub(r"\b(?:[0-9A-Fa-f]{2}[:-]){5}(?:[0-9A-Fa-f]{2})\b", mask_mac, text)

    # Aðrar IPv4 tölur á heimaneti eða Tailscale
    lan_ip_counter = 1
    def mask_lan_ip(m):
        nonlocal lan_ip_counter
        val = m.group(0)
        if val in ("127.0.0.1", "0.0.0.0", "255.255.255.255", "192.168.68.255"):
            return val
        for k, v in mapping.items():
            if v == val:
                return k
        token = f"<LAN_IP_{lan_ip_counter}>"
        lan_ip_counter += 1
        mapping[token] = val
        return token

    text = re.sub(r"\b(?:192\.168\.\d{1,3}\.\d{1,3}|10\.\d{1,3}\.\d{1,3}\.\d{1,3}|100\.\d{1,3}\.\d{1,3}\.\d{1,3})\b", mask_lan_ip, text)

    # Notendanöfn úr USER.md ef til staðar
    user_md = os.path.join(WORKSPACE_DIR, "USER.md")
    if os.path.exists(user_md):
        try:
            with open(user_md, "r", encoding="utf-8") as f:
                for line in f:
                    if "nafn" in line.lower() or "name" in line.lower():
                        parts = line.split(":")
                        if len(parts) > 1:
                            raw_name = parts[1].strip()
                            if len(raw_name) >= 3:
                                mapping["<USER>"] = raw_name
                                text = re.sub(re.escape(raw_name), "<USER>", text, flags=re.IGNORECASE)
        except Exception:
            pass

    return text, mapping


def desanitize_from_external_llm(text: str, mapping: dict) -> str:
    """Afkóðar tákn úr vefsvara til baka í raunveruleg heiti/vistföng."""
    for token, orig in mapping.items():
        text = text.replace(token, orig)
        bare = token.strip("<>")
        text = re.sub(rf"\b{re.escape(bare)}\b", orig, text)
    return text


def gather_context_for_prompt(prompt: str) -> str:
    """Safnar viðeigandi stöðu staðvært miðað við lykilorð í fyrirspurn."""
    low = prompt.lower()
    ctx_parts = []

    if any(k in low for k in ["rafhlöð", "rafhlaða", "hiti", "status", "staða", "stada", "hlaða", "power", "uptime"]):
        try:
            ctx_parts.append("RAUNTÍMASTAÐA KERFIS:\n" + tool_get_r8q_status())
        except Exception as e:
            ctx_parts.append(f"Kerfisstöðuvilla: {e}")

    if any(k in low for k in ["tab", "tab4", "taba", "spjaldtölv", "spjaldtolv", "net", "host", "ip", "local", "ping", "arps"]):
        try:
            target_to_inspect = "tab4" if any(k in low for k in ["tab", "spjald"]) else ""
            ctx_parts.append("NET- OG TÆKJASTAÐA:\n" + tool_inspect_lan_host(target_to_inspect))
        except Exception as e:
            ctx_parts.append(f"Netstöðuvilla: {e}")

    if any(k in low for k in ["öryggi", "oryggi", "uppfærsl", "uppfaersl", "pakkar", "pakki", "cve"]):
        try:
            ctx_parts.append("ÖRYGGIS- OG KERFISUPPFÆRSLUR:\n" + tool_get_security_updates())
        except Exception as e:
            ctx_parts.append(f"Öryggisstöðuvilla: {e}")

    if any(k in low for k in ["kuma", "vakt", "incident", "downtime", "niðritími"]):
        try:
            ctx_parts.append("UPTIME KUMA VAKTIR:\n" + tool_get_uptime_kuma_status())
        except Exception as e:
            ctx_parts.append(f"Kuma villa: {e}")

    if not ctx_parts:
        try:
            ctx_parts.append("GRUNNSTAÐA SÍMA:\n" + tool_get_r8q_status())
        except Exception:
            pass

    return "\n\n".join(ctx_parts)


def trigger_prompt_relay(text: str, env: dict) -> str:
    """Útbýr nafnlausan prompt-pakka fyrir ókeypis vefmódel þegar API kvóti er uppurinn."""
    raw_ctx = gather_context_for_prompt(text)
    combined_raw = f"{text}\n---\n{raw_ctx}"
    _, mapping = sanitize_for_external_llm(combined_raw)

    sanitized_prompt, _ = sanitize_for_external_llm(text)
    sanitized_context, _ = sanitize_for_external_llm(raw_ctx)

    state = {
        "timestamp": time.time(),
        "original_prompt": text,
        "sanitized_prompt": sanitized_prompt,
        "mapping": mapping,
    }
    try:
        with open(PENDING_RELAY_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as exc:
        print(f"[openclaw-relay] Villa við að vista relay stöðu: {exc}", flush=True)

    relay_msg = (
        "⚠️ **Kvóti á AI Studio og Groq er búinn (HTTP 429).**\n\n"
        "Hér er tilbúinn nafnlaus forsendu- og spurningapakki sem þú getur afritað og límt í ókeypis vefmódel "
        "(Claude, ChatGPT, Grok eða Meta AI):\n\n"
        "```markdown\n"
        "Þú ert OpenClaw, persónulegur gervigreindaraðstoðarmaður á Linux netþjóni.\n"
        "Svaraðu á góðri íslensku á hnitmiðaðan og hjálplegan hátt fyrir símaskjá.\n\n"
        f"FORSENDUR FRÁ KERFINU:\n{sanitized_context}\n\n"
        f"SPURNING NOTANDA:\n{sanitized_prompt}\n"
        "```\n\n"
        "👉 **Til að svara:** Afritaðu pakkann hér að ofan, límdu í vefmódelið og sendu svarið til baka hingað í ntfy "
        "(eða byrjaðu á `Svar: ...`). OpenClaw afkóðar táknin og vistar í samtalið."
    )
    return relay_msg


def ask_openclaw(prompt: str, env: dict) -> tuple[str, str]:
    text = prompt.strip()
    if text.lower() in ("/reset", "/clear", "hreinsa minni"):
        save_history([])
        if os.path.exists(PENDING_RELAY_FILE):
            try:
                os.remove(PENDING_RELAY_FILE)
            except Exception:
                pass
        return "Minni samtalsins hefur verið hreinsað.", "system"

    api_key = env.get("GEMINI_API_KEY", "").strip()
    groq_api_key = env.get("GROQ_API_KEY", "").strip()

    system_prompt = load_workspace_prompt()
    history = load_history()
    contents = list(history) + [{"role": "user", "parts": [{"text": text}]}]

    seen_calls = set()
    used_model = MODELS[0]
    gemini_enabled = bool(api_key)

    try:
        for step in range(MAX_TOOL_ROUNDS + 1):
            is_final_step = (step == MAX_TOOL_ROUNDS)
            if is_final_step:
                contents.append({
                    "role": "user",
                    "parts": [{"text": "[KERFISBOÐ: Hámarki verkfærakalla náð. Dragðu saman skýrt, hjálplegt lokasvar á íslensku handa notandanum út frá þeim upplýsingum sem þú hefur þegar safnað hér að ofan.]"}],
                })

            res = None
            # 1. Reyna Gemini fyrst
            if gemini_enabled:
                try:
                    res, used_model = call_gemini(api_key, contents, system_prompt, enable_tools=not is_final_step)
                except Exception as e_gemini:
                    print(f"[openclaw-bridge] Gemini mistókst ({e_gemini}).", flush=True)
                    gemini_enabled = False
                    res = None

            # 2. Ef Gemini brást: Reyna Groq (Fallback 1)
            if res is None and groq_api_key:
                try:
                    print("[openclaw-bridge] Reyni Groq (llama-3.3-70b-versatile)...", flush=True)
                    res, used_model = call_groq(groq_api_key, contents, system_prompt, enable_tools=not is_final_step)
                except Exception as e_groq:
                    print(f"[openclaw-bridge] Groq mistókst ({e_groq}).", flush=True)
                    res = None

            # 3. Ef bæði bregðast: Reyna Local Llama (ef í næturglugga 23:30 - 07:10)
            if res is None and is_night_window():
                try:
                    print("[openclaw-bridge] Reyni Local Llama (næturgluggi)...", flush=True)
                    res_llama, used_model = call_local_llama(contents, system_prompt)
                    llama_text = "🌙 (staðvært líkan, enska)\n" + res_llama.get("text", "")
                    new_hist = list(history) + [
                        {"role": "user", "parts": [{"text": text}]},
                        {"role": "model", "parts": [{"text": llama_text}]},
                    ]
                    save_history(new_hist)
                    return llama_text, used_model
                except LocalLlamaUnavailable as e_llama:
                    print(f"[openclaw-bridge] Local Llama féll ({e_llama.reason}), held áfram í Relay...", flush=True)
                    res = None

            # 4. Ef öll LLM brugðust: Fallback 3 (Human-in-the-Loop Relay)
            if res is None:
                print("[openclaw-bridge] Öll LLM brugðust -> Virkja Fallback 3 Prompt-Relay...", flush=True)
                return trigger_prompt_relay(text, env), "prompt-relay"

            cand = (res.get("candidates") or [{}])[0]
            content = cand.get("content") or {}
            parts = content.get("parts") or []
            if not parts:
                return "(Ekkert svar barst frá líkani)", used_model

            fn_calls = [p["functionCall"] for p in parts if "functionCall" in p]
            if not fn_calls or is_final_step:
                reply_text = "\n".join(p.get("text", "") for p in parts if "text" in p).strip()
                if reply_text:
                    new_hist = list(history) + [
                        {"role": "user", "parts": [{"text": text}]},
                        {"role": "model", "parts": [{"text": reply_text}]},
                    ]
                    save_history(new_hist)
                return reply_text or "(Tómt svar)", used_model

            contents.append({"role": "model", "parts": parts})
            fn_responses = []
            for fc in fn_calls:
                fn_name = fc.get("name", "")
                fn_args = fc.get("args") or {}
                call_sig = (fn_name, json.dumps(fn_args, sort_keys=True))
                if call_sig in seen_calls:
                    tool_out = "[Kerfisboð: Þetta verkfærakall hefur þegar verið keyrt í þessari lotu. Ekki kalla á það aftur heldur svaraðu notandanum.]"
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

        return "Náði hámarki lotu.", used_model
    except Exception as exc:
        print(f"[openclaw-bridge] Óvænt villa í ask_openclaw: {exc}", flush=True)
        try:
            return trigger_prompt_relay(text, env), "prompt-relay"
        except Exception:
            return f"Villa í OpenClaw: {exc}", used_model


def post_ntfy(message: str, env: dict, title: str = "🤖 OpenClaw", tags: str = "robot_face", priority: str = "default", actions: str = "") -> None:
    ntfy_url = env.get("NTFY_URL", "http://127.0.0.1:2586").rstrip("/")
    ntfy_topic = env.get("NTFY_TOPIC", "openclaw")
    ntfy_token = env.get("NTFY_TOKEN", "")
    url = f"{ntfy_url}/{ntfy_topic}"
    req = urllib.request.Request(url, data=message.encode("utf-8"), method="POST")
    b64_title = base64.b64encode(title.encode("utf-8")).decode("ascii")
    req.add_header("Title", f"=?utf-8?b?{b64_title}?=")

    tag_list = [t.strip() for t in tags.split(",") if t.strip()]
    if "robot_face" not in tag_list:
        tag_list.append("robot_face")
    req.add_header("Tags", ",".join(tag_list))

    if priority != "default":
        req.add_header("Priority", priority)
    if actions:
        req.add_header("Actions", actions)
    if ntfy_token:
        req.add_header("Authorization", f"Bearer {ntfy_token}")
    with urllib.request.urlopen(req, timeout=15) as resp:
        resp.read()


LAST_TRIAGE_HASH_FILE = "/tmp/openclaw_last_triage.hash"


def build_triage_actions(env: dict) -> str:
    # Nota opinbera Tailscale Funnel slóð svo Action hnappar virki á Pixel símanum bæði á Wi-Fi og LTE
    base_url = env.get("PUBLIC_NTFY_URL", PUBLIC_NTFY_URL).rstrip("/")
    ntfy_topic = env.get("NTFY_TOPIC", "openclaw")
    ntfy_token = env.get("NTFY_TOKEN", "")
    target_endpoint = f"{base_url}/{ntfy_topic}"
    auth_header = f", headers.Authorization=Bearer {ntfy_token}" if ntfy_token else ""
    return (
        f"http, Uppfaera strax, {target_endpoint}, method=POST, body=@cmd:confirm_update{auth_header}; "
        f"http, Greina ahaettu, {target_endpoint}, method=POST, body=Utskyrdu nanar ahaettuna af thessum pokkum a r8q.{auth_header}"
    )


def handle_confirm_update(env: dict) -> None:
    """Tekur við @cmd:confirm_update frá ntfy hnappi og ræsir uppfærslu sem rót."""
    status_file = "/var/cache/r8q_update_status.json"
    if not os.path.exists(status_file):
        post_ntfy("⚠️ Villa: Stöðuskrá fannst ekki. Get ekki virkjað uppfærslu.", env, title="⚠️ Uppfærsla stöðvuð", tags="warning")
        return
    try:
        with open(status_file, "r", encoding="utf-8") as f:
            sdata = json.load(f)
        token = sdata.get("token")
        if not token:
            post_ntfy("⚠️ Villa: Ekkert öryggistákn fannst í stöðuskrá.", env, title="⚠️ Uppfærsla stöðvuð", tags="warning")
            return

        url = f"http://127.0.0.1:8080/api/update-confirm?token={token}&confirm=1"
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=20) as resp:
            resp.read()

        post_ntfy(
            "🚀 Kerfisuppfærsla hefur verið staðfest og virkjuð á R8Q!\n"
            "Pakkastjórinn keyrir nú í bakgrunni sem rót. Þú færð tilkynningu um leið og henni lýkur.",
            env,
            title="⚡ Uppfærsla hafin",
            tags="rocket"
        )
    except Exception as exc:
        post_ntfy(f"⚠️ Villa við að virkja uppfærslu: {exc}", env, title="⚠️ Uppfærsluvilla", tags="warning")


def run_proactive_triage(payload: dict) -> None:
    """Framkvæmir staka, létta Proactive AI Triage greiningu á öryggisstöðu r8q og sendir ntfy með hnöppum."""
    import gc
    status_file = "/var/cache/r8q_update_status.json"
    if not os.path.exists(status_file):
        print("[openclaw-triage] Stöðuskrá finnst ekki, hætti við.", flush=True)
        return

    # Minnisvörn (cgroup guard): staðfesta að skrá sé undir 32KB
    file_size = os.path.getsize(status_file)
    if file_size > 32768:
        print(f"[openclaw-triage] Stöðuskrá of stór ({file_size} bæti > 32KB), hætti við til að verja minni.", flush=True)
        return

    try:
        with open(status_file, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as exc:
        print(f"[openclaw-triage] Villa við lestur stöðuskrár: {exc}", flush=True)
        return

    triage_hash = data.get("triage_hash") or payload.get("hash") or ""
    env = load_env()

    # Kvótavörn: Athuga hvort sama triage_hash hafi þegar verið greint
    if triage_hash:
        try:
            if os.path.exists(LAST_TRIAGE_HASH_FILE):
                with open(LAST_TRIAGE_HASH_FILE, "r") as f:
                    last_hash = f.read().strip()
                if last_hash == triage_hash and not payload.get("force"):
                    print("[openclaw-triage] Sama triage_hash og síðast, sleppi Gemini kalli.", flush=True)
                    post_ntfy(
                        "ℹ️ Áminning: Uppfærslur bíða enn eftir staðfestingu á R8Q.",
                        env,
                        title="📦 Kerfisuppfærslur í bið",
                        tags="package",
                        priority="default",
                        actions=build_triage_actions(env)
                    )
                    return
        except Exception:
            pass

    vuln_pkgs = data.get("vulnerable_packages", [])
    routine = data.get("routine_packages", {})
    sys_ctx = data.get("system_context", {})
    ports = [f"{p['proto'].upper()}/{p['port']} ({p['process']})" for p in sys_ctx.get("listening_ports", []) if p.get("port")]
    services = sys_ctx.get("running_services", [])[:15]
    tablet_checks = data.get("tablet", {}).get("checks", {})
    tablet_sec_ok = data.get("tablet", {}).get("security_ok", True)
    tab_summary = f"Öryggi OK={tablet_sec_ok}, Rafhlaða={tablet_checks.get('tablet_battery_pct', 'N/A')}%, Hiti={tablet_checks.get('tablet_temp_c', 'N/A')}°C"

    triage_input = {
        "vulnerable_packages": vuln_pkgs,
        "routine_packages_count": routine.get("count", 0),
        "routine_core_matches": routine.get("core_matches", []),
        "open_listening_ports": ports[:20],
        "running_services_sample": services,
        "galaxy_tab_status": tab_summary
    }

    system_instruction = (
        "Þú ert OpenClaw, staðværi öryggisvörðurinn á Samsung Galaxy S20 FE 5G (r8q). "
        "Greindu CVE og pakkalistann gegn virkum portum í open_listening_ports og running_services_sample á r8q. "
        "Gefðu nákvæmlega 3–5 hnitmiðaðar línur á góðri íslensku:\n"
        "1) Heildaráhættumat: (LÁGT / MIÐLUNGS / HÁTT) með einni röksemd.\n"
        "2) Hvaða pakkar snerta opin port, netþjónustur eða kjarna (t.d. SSH, Tailscale, DNS, Linux kjarni).\n"
        "3) Staða Galaxy Tab 4 spjaldtölvunnar.\n"
        "Svarið birtist í ntfy tilkynningu á símaskjá, hafðu það skýrt, faglegt og án óþarfa málalenginga."
    )

    contents = [{"role": "user", "parts": [{"text": json.dumps(triage_input, ensure_ascii=False)}]}]
    api_key = env.get("GEMINI_API_KEY", "").strip()

    triage_text = ""
    model_used = "gemini"
    try:
        reply_dict, model_used = call_gemini(api_key, contents, system_instruction, enable_tools=False, max_output_tokens=450)
        candidates = reply_dict.get("candidates", [])
        if candidates:
            parts = candidates[0].get("content", {}).get("parts", [])
            triage_text = "".join(p.get("text", "") for p in parts if "text" in p).strip()
    except Exception as exc:
        print(f"[openclaw-triage] Gemini kall mistókst: {exc}", flush=True)
        groq_api_key = env.get("GROQ_API_KEY", "").strip()
        if groq_api_key:
            try:
                print("[openclaw-triage] Reyni Groq í triage...", flush=True)
                reply_dict, model_used = call_groq(groq_api_key, contents, system_instruction, enable_tools=False)
                candidates = reply_dict.get("candidates", [])
                if candidates:
                    parts = candidates[0].get("content", {}).get("parts", [])
                    triage_text = "".join(p.get("text", "") for p in parts if "text" in p).strip()
            except Exception as e_groq:
                print(f"[openclaw-triage] Groq triage mistókst líka: {e_groq}", flush=True)
        triage_text = f"Öryggisuppfærslur bíða ({len(vuln_pkgs)} veikleikar, {routine.get('count', 0)} almennir pakkar). Spjaldtölva: {tab_summary}."
    finally:
        gc.collect()

    # Vistum triage_hash
    if triage_hash:
        try:
            with open(LAST_TRIAGE_HASH_FILE, "w") as f:
                f.write(triage_hash)
        except Exception:
            pass

    actions = build_triage_actions(env)
    urgency = data.get("urgency", "normal")
    post_ntfy(
        triage_text,
        env,
        title=f"🛡️ OpenClaw: Öryggismat ({model_used})",
        tags="shield,warning" if urgency == "urgent" else "shield,package",
        priority="high" if urgency == "urgent" else "default",
        actions=actions
    )
    print(f"[openclaw-triage] Triage tilkynning send ({model_used}).", flush=True)


def hardware_watchdog_loop() -> None:
    """Fylgist stöðugt með rafmagni (hleðslutæki úr sambandi/rafmagnsleysi), hita og rafhlöðu."""
    print("[openclaw-watchdog] Vélbúnaðar- og rafmagnsgæsla ræst...", flush=True)
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
                        msg = f"⚠️ Rafmagn fór af heimilinu (eða hleðslutæki tekið úr sambandi)!\nSíminn keyrir nú á innbyggðri rafhlöðu ({cap}% eftir)."
                        post_ntfy(msg, env, title="⚡ Rafmagnsleysi / Rafhlaða", tags="warning,electric_plug", priority="high")
                    else:
                        msg = f"✅ Rafmagn komst aftur á!\nSíminn er aftur í hleðslu ({cap}% staða)."
                        post_ntfy(msg, env, title="🔌 Rafmagn komið á", tags="white_check_mark,zap", priority="default")
                    last_charger_state = charger_online

            temp_c = 0
            for p in glob.glob("/sys/class/power_supply/*battery*/temp"):
                try:
                    temp_c = int(open(p).read().strip()) / 10.0
                except Exception:
                    pass
            if temp_c >= 45.0 and (time.time() - last_temp_alert_time > 1800):
                msg = f"🔥 Hitaviðvörun á r8q símanum!\nHitastig rafhlöðu er komið í {temp_c:.1f}°C."
                post_ntfy(msg, env, title="🔥 Hitaviðvörun", tags="fire,warning", priority="high")
                last_temp_alert_time = time.time()

        except Exception as exc:
            print(f"[openclaw-watchdog] Villa í eftirliti: {exc}", flush=True)

        time.sleep(10)


def listen_loop() -> None:
    print("[openclaw-ntfy-bridge] Ræsi létta OpenClaw ntfy-brú v4 (Wake-on-LAN + Wake-burst)...", flush=True)
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
                    if (
                        "robot_face" in tags
                        or title.startswith("🤖")
                        or title.startswith("🛡️")
                        or title.startswith("⚡")
                        or title.startswith("📦")
                        or "warning" in tags
                        or "white_check_mark" in tags
                    ):
                        continue
                    if not msg:
                        continue

                    # Athuga hvort virkt Prompt-Relay svar sé að berast frá notanda
                    if os.path.exists(PENDING_RELAY_FILE):
                        try:
                            with open(PENDING_RELAY_FILE, "r", encoding="utf-8") as f:
                                relay_state = json.load(f)
                            relay_ts = relay_state.get("timestamp", 0)
                            if time.time() - relay_ts < 1800:
                                is_explicit_answer = msg.lower().startswith("svar:")
                                is_cancel = msg.lower().strip() in ("/cancel", "hætta við", "haetta vid", "/clear", "/reset")
                                if is_cancel:
                                    try:
                                        os.remove(PENDING_RELAY_FILE)
                                    except Exception:
                                        pass
                                    post_ntfy("Handvirkri varalínu var aflýst.", env, title="🤖 OpenClaw", tags="wastebasket")
                                    continue
                                elif is_explicit_answer or (not msg.startswith("/") and not msg.startswith("@cmd:")):
                                    ans_text = msg
                                    if is_explicit_answer:
                                        ans_text = msg.split(":", 1)[1].strip()
                                    mapping = relay_state.get("mapping", {})
                                    decoded_answer = desanitize_from_external_llm(ans_text, mapping)

                                    hist = load_history()
                                    new_hist = list(hist) + [
                                        {"role": "user", "parts": [{"text": relay_state.get("original_prompt", "Fyrirspurn")}]},
                                        {"role": "model", "parts": [{"text": decoded_answer}]},
                                    ]
                                    save_history(new_hist)
                                    try:
                                        os.remove(PENDING_RELAY_FILE)
                                    except Exception:
                                        pass

                                    post_ntfy(
                                        f"✅ Svarið hefur verið afkóðað og vistað í samtalssöguna:\n\n{decoded_answer}",
                                        env,
                                        title="🤖 OpenClaw (Handvirk varalína - Samþykkt)",
                                        tags="white_check_mark"
                                    )
                                    continue
                            else:
                                try:
                                    os.remove(PENDING_RELAY_FILE)
                                except Exception:
                                    pass
                        except Exception as exc:
                            print(f"[openclaw-relay] Villa við meðhöndlun relay svars: {exc}", flush=True)
# Greina innri atburði og kerfisskipanir
                    if "SYS_TRIAGE_TRIGGER" in msg:
                        print(f"[openclaw-ntfy-bridge] Móttekið SYS_TRIAGE_TRIGGER: {msg[:100]}", flush=True)
                        try:
                            payload = json.loads(msg) if msg.startswith("{") else {}
                        except Exception:
                            payload = {}
                        run_proactive_triage(payload)
                        continue

                    if msg.startswith("@cmd:confirm_update"):
                        print("[openclaw-ntfy-bridge] Móttekin uppfærslustaðfesting (@cmd:confirm_update)...", flush=True)
                        handle_confirm_update(env)
                        continue

                    # Fast-Path Router (Þrep 0: 0 tokens, < 50ms)
                    fast = fastpath.try_fast_path(
                        msg, progress=lambda m: post_ntfy(m, env, title="OpenClaw", tags="hourglass")
                    )
                    if fast is not None:
                        post_ntfy(fast.text, env, title=fast.title, tags=",".join(fast.tags) if fast.tags else "robot_face")
                        print(f"[openclaw-bridge] Fast-path {fast.intents} {fast.elapsed_ms:.1f} ms", flush=True)
                        continue

                    print(f"[openclaw-ntfy-bridge] Fyrirspurn: {msg[:80]}", flush=True)
                    reply, model = ask_openclaw(msg, load_env())
                    title = "📋 OpenClaw (Handvirk varalína)" if model == "prompt-relay" else f"🤖 OpenClaw ({model})"
                    tags = "clipboard,robot_face" if model == "prompt-relay" else "robot_face"
                    post_ntfy(reply, env, title=title, tags=tags)
        except Exception as exc:
            print(f"[openclaw-ntfy-bridge] Endurtengist ntfy straumi ({exc})", flush=True)
            time.sleep(2)


if __name__ == "__main__":
    listen_loop()
