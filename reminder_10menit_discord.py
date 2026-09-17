#!/usr/bin/env python3
"""
Reminder 10 menit SEBELUM tiket overdue -> Discord (pakai Webhook).

Cara pakai:
    Windows PowerShell:
        $env:SDP_AUTHTOKEN="token_sdp_kamu"
        $env:DISCORD_WEBHOOK_URL="url_webhook_discord"
        python reminder_10menit_discord.py            # kirim beneran
        python reminder_10menit_discord.py --dry-run  # cuma print

Jadwalkan tiap 5 menit lewat Task Scheduler / cron / GitHub Actions.
"""

import os
import sys
import json
import time
import datetime
import requests

# ---------------------------------------------------------------- KONFIGURASI

SDP_BASE_URL = os.getenv("SDP_BASE_URL", "https://servicedesk.apps.binus.edu")
SDP_AUTHTOKEN = os.getenv("SDP_AUTHTOKEN")

DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")

REMINDER_MINUTES_BEFORE = 10
WINDOW_TOLERANCE_MINUTES = 5

CLOSED_STATUSES = ["Closed", "Resolved", "Cancelled"]
VERIFY_SSL = os.getenv("SDP_VERIFY_SSL", "true").lower() != "false"

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sudah_direminder.json")
STATE_RETENTION_HOURS = 24

DRY_RUN = "--dry-run" in sys.argv

# ------------------------------------------------------------------ AMBIL SDP


def fetch_soon_due_requests():
    now = datetime.datetime.now()
    window_start_ms = int(now.timestamp() * 1000)
    window_end = now + datetime.timedelta(
        minutes=REMINDER_MINUTES_BEFORE + WINDOW_TOLERANCE_MINUTES
    )
    window_end_ms = int(window_end.timestamp() * 1000)

    url = f"{SDP_BASE_URL}/api/v3/requests"
    headers = {
        "authtoken": SDP_AUTHTOKEN,
        "Accept": "application/vnd.manageengine.sdp.v3+json",
    }

    input_data = {
        "list_info": {
            "row_count": 100,
            "sort_field": "due_by_time",
            "sort_order": "asc",
            "search_criteria": [
                {"field": "status.name", "condition": "is not", "values": CLOSED_STATUSES},
                {
                    "field": "due_by_time",
                    "condition": "greater than",
                    "value": str(window_start_ms),
                    "logical_operator": "AND",
                },
                {
                    "field": "due_by_time",
                    "condition": "lt",
                    "value": str(window_end_ms),
                    "logical_operator": "AND",
                },
            ],
        }
    }

    resp = requests.get(
        url,
        headers=headers,
        params={"input_data": json.dumps(input_data)},
        verify=VERIFY_SSL,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json().get("requests", [])


def minutes_until_due(req):
    due = (req.get("due_by_time") or {}).get("value")
    if not due:
        return None
    due_dt = datetime.datetime.fromtimestamp(int(due) / 1000)
    return round((due_dt - datetime.datetime.now()).total_seconds() / 60)


# ------------------------------------------------------------------ STATE


def load_state():
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


def cleanup_state(state):
    cutoff = time.time() - (STATE_RETENTION_HOURS * 3600)
    return {tid: ts for tid, ts in state.items() if ts > cutoff}


# -------------------------------------------------------------- SUSUN PESAN


def build_embed(req):
    """
    Discord mendukung 'embed' (kartu berwarna) yang lebih rapi daripada
    teks polos. Warna disesuaikan tingkat urgensi (makin dekat due,
    makin merah).
    """
    ticket_id = req.get("id")
    subject = (req.get("subject") or "(tanpa subjek)").strip()
    requester = (req.get("requester") or {}).get("name", "-")
    tech = (req.get("technician") or {}).get("name", "belum di-assign")
    priority = (req.get("priority") or {}).get("name", "-")
    menit_lagi = minutes_until_due(req)
    link = f"{SDP_BASE_URL}/WorkOrder.do?woMode=viewWO&woID={ticket_id}"

    # Warna: <=3 menit merah, <=6 menit oranye, selebihnya kuning.
    if menit_lagi is not None and menit_lagi <= 3:
        color = 0xE74C3C
    elif menit_lagi is not None and menit_lagi <= 6:
        color = 0xE67E22
    else:
        color = 0xF1C40F

    return {
        "title": f"⏰ Tiket #{ticket_id} akan overdue dalam {menit_lagi} menit",
        "description": subject,
        "url": link,
        "color": color,
        "fields": [
            {"name": "Requester", "value": requester, "inline": True},
            {"name": "Teknisi", "value": tech, "inline": True},
            {"name": "Prioritas", "value": priority, "inline": True},
        ],
        "footer": {"text": "SDP Overdue Reminder"},
    }


# --------------------------------------------------------------- KIRIM DISCORD


def send_discord(embed):
    if DRY_RUN:
        print(f"\n===== [DRY RUN] kirim ke Discord =====\n{json.dumps(embed, indent=2, ensure_ascii=False)}\n")
        return True

    resp = requests.post(
        DISCORD_WEBHOOK_URL,
        json={"embeds": [embed]},
        timeout=30,
    )
    ok = resp.status_code in (200, 204)
    if not ok:
        print(f"[GAGAL] kirim ke Discord ({resp.status_code}): {resp.text}")
    return ok


# -------------------------------------------------------------------- MAIN


def main():
    if not SDP_AUTHTOKEN:
        sys.exit("SDP_AUTHTOKEN belum di-set.")
    if not DRY_RUN and not DISCORD_WEBHOOK_URL:
        sys.exit("DISCORD_WEBHOOK_URL belum di-set.")

    try:
        tickets = fetch_soon_due_requests()
    except Exception as e:
        sys.exit(f"Gagal ambil data dari SDP: {e}")

    state = cleanup_state(load_state())

    if not tickets:
        print("Tidak ada tiket yang mendekati due date saat ini.")
        save_state(state)
        return

    terkirim = 0
    for req in tickets:
        ticket_id = str(req.get("id"))
        if ticket_id in state:
            continue

        menit_lagi = minutes_until_due(req)
        if menit_lagi is None or menit_lagi > REMINDER_MINUTES_BEFORE:
            continue

        if send_discord(build_embed(req)):
            state[ticket_id] = time.time()
            terkirim += 1
            time.sleep(1)

    save_state(state)
    print(f"Selesai. {terkirim} reminder terkirim dari {len(tickets)} tiket dalam window.")


if __name__ == "__main__":
    main()
