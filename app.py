import os
import json
import re
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from flask import Flask, request
import gspread
from google.oauth2.service_account import Credentials


BOT_TOKEN = os.environ["BOT_TOKEN"]
SPREADSHEET_ID = os.environ["SPREADSHEET_ID"]
GOOGLE_CREDENTIALS = os.environ["GOOGLE_CREDENTIALS"]
TIMEZONE = os.environ.get("TIMEZONE", "Asia/Kuala_Lumpur")

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

SHEET_TRANSACTIONS = "Transactions"
SHEET_PLAYER_SUMMARY = "Player_Summary"
SHEET_BANK_LIST = "Bank_List"

ACTIVE_STATUS = {"ACTIVE"}
STOP_STATUS = {"STOP", "LIMIT", "ISSUE", "INACTIVE", "OFF", "CLOSED", "DISABLED"}

app = Flask(__name__)


def clean_text(x):
    return str(x or "").strip()


def normalize(x):
    return clean_text(x).lower()


def get_sheet():
    creds_dict = json.loads(GOOGLE_CREDENTIALS)
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    client = gspread.authorize(creds)
    return client.open_by_key(SPREADSHEET_ID)


def send_message(chat_id, text, reply_to_message_id=None):
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
    }
    if reply_to_message_id:
        payload["reply_to_message_id"] = reply_to_message_id
    requests.post(f"{TELEGRAM_API}/sendMessage", json=payload, timeout=10)


def parse_command(text):
    parts = clean_text(text).split()
    if not parts:
        return "", []
    return parts[0].lower(), parts[1:]


def get_bank_status_map():
    """
    Bank_List format expected:
    Row 3 header:
    A = Bank Name
    B = Code
    C = Status

    Data starts row 4.
    """
    ss = get_sheet()
    ws = ss.worksheet(SHEET_BANK_LIST)
    rows = ws.get_all_values()

    bank_map = {}

    for row in rows[3:]:
        if not row:
            continue

        bank_name = clean_text(row[0]) if len(row) > 0 else ""
        status = clean_text(row[2]) if len(row) > 2 else ""

        if not bank_name:
            continue

        if not status:
            status = "ACTIVE"

        bank_map[normalize(bank_name)] = {
            "name": bank_name,
            "status": status.upper(),
        }

    return bank_map


def is_bank_active(bank_name, bank_map=None):
    if bank_map is None:
        bank_map = get_bank_status_map()

    item = bank_map.get(normalize(bank_name))

    # If bank is not found in Bank_List, treat as NOT active to avoid CS using unknown bank.
    if not item:
        return False, "NOT FOUND"

    status = clean_text(item.get("status")).upper()
    return status in ACTIVE_STATUS, status


def find_summary_row(player):
    ss = get_sheet()
    ws = ss.worksheet(SHEET_PLAYER_SUMMARY)
    rows = ws.get_all_values()

    if not rows or len(rows) < 4:
        return None, None

    headers = rows[2]  # row 3
    player_key = normalize(player)

    for row in rows[3:]:
        if row and normalize(row[0]) == player_key:
            return headers, row

    return headers, None


def get_summary_value(headers, row, header_name):
    for i, h in enumerate(headers):
        if normalize(h) == normalize(header_name):
            return clean_text(row[i]) if i < len(row) else ""
    return ""


def get_bank_status_from_summary(headers, row, bank_name):
    bank_key = normalize(bank_name)

    for i, h in enumerate(headers):
        if normalize(h) == bank_key:
            return clean_text(row[i]) if i < len(row) else ""

    return None


def split_bank_list(value):
    if not value or value == "-":
        return []

    parts = [clean_text(x) for x in str(value).split(",")]
    return [x for x in parts if x]


def get_allowed_active_wd_banks(headers, row):
    """
    Takes allowed WD banks from Player_Summary,
    then filters only Bank_List status ACTIVE.
    """
    bank_map = get_bank_status_map()

    allowed_raw = get_summary_value(headers, row, "Allowed WD Banks")
    allowed_banks = split_bank_list(allowed_raw)

    active_allowed = []
    hidden_banks = []

    for bank in allowed_banks:
        active, status = is_bank_active(bank, bank_map)

        if active:
            active_allowed.append(bank)
        else:
            hidden_banks.append(f"{bank} ({status})")

    return active_allowed, hidden_banks


def check_player(player):
    headers, row = find_summary_row(player)
    if not row:
        return f"❌ Player not found:\n<b>{player}</b>"

    count = (
        get_summary_value(headers, row, "Used Deposit Bank Count")
        or get_summary_value(headers, row, "Used Deposit Bank")
        or "0"
    )
    used = get_summary_value(headers, row, "Deposit Banks Used") or "-"
    control = get_summary_value(headers, row, "Control") or "-"

    active_allowed, hidden_banks = get_allowed_active_wd_banks(headers, row)

    allowed_text = "\n".join(active_allowed) if active_allowed else "-"
    hidden_text = ""
    if hidden_banks:
        hidden_text = "\n\n🚫 Hidden STOP/LIMIT banks:\n" + "\n".join(hidden_banks)

    return (
        f"🔎 <b>Player Check</b>\n\n"
        f"👤 Player: <b>{clean_text(row[0])}</b>\n"
        f"🏦 Used Deposit Bank Count: <b>{count}</b>\n\n"
        f"📥 Deposit Banks Used:\n{used}\n\n"
        f"📤 Allowed WD Banks ACTIVE only:\n{allowed_text}"
        f"{hidden_text}\n\n"
        f"⚠️ Status:\n{control}"
    )


def append_transaction(player, tx_type, amount, bank, entered_by="BOT", remark=""):
    ss = get_sheet()
    ws = ss.worksheet(SHEET_TRANSACTIONS)

    today = datetime.now(ZoneInfo(TIMEZONE)).strftime("%Y-%m-%d %H:%M:%S")

    # A Date | B Player | C Type | D Bank | E Amount | F Remark | G Entered By
    ws.append_row(
        [today, player, tx_type, bank, amount, remark, entered_by],
        value_input_option="USER_ENTERED",
    )


def bank_was_used_for_deposit(player, bank):
    headers, row = find_summary_row(player)
    if not row:
        return False, None

    status = get_bank_status_from_summary(headers, row, bank)

    if status is None:
        return False, None

    return normalize(status) == "dep used", status


def handle_dep(args, username):
    if len(args) < 3:
        return (
            "❌ Format salah.\n\n"
            "Gunakan:\n"
            "<code>/dep player amount bank</code>\n\n"
            "Contoh:\n"
            "<code>/dep Jimmy88 500 ANEXT HORIZON</code>"
        )

    player = args[0]
    amount = args[1]
    bank = " ".join(args[2:])

    if not re.match(r"^\d+(\.\d{1,2})?$", amount.replace(",", "")):
        return "❌ Amount tidak valid."

    active, bank_status = is_bank_active(bank)

    if not active:
        return (
            f"❌ <b>DEPOSIT REJECTED</b>\n\n"
            f"🏦 Bank: <b>{bank}</b>\n"
            f"Status: <b>{bank_status}</b>\n\n"
            f"Reason: Bank is not ACTIVE in Bank_List."
        )

    amount = amount.replace(",", "")

    append_transaction(
        player=player,
        tx_type="Deposit",
        amount=amount,
        bank=bank,
        entered_by=username,
        remark="Telegram Bot Deposit",
    )

    return (
        f"✅ <b>Deposit recorded</b>\n\n"
        f"👤 Player: <b>{player}</b>\n"
        f"💰 Amount: <b>{amount}</b>\n"
        f"🏦 Bank: <b>{bank}</b>\n\n"
        f"Use <code>/check {player}</code> to verify."
    )


def handle_wd(args, username):
    if len(args) < 3:
        return (
            "❌ Format salah.\n\n"
            "Gunakan:\n"
            "<code>/wd player amount bank</code>\n\n"
            "Contoh:\n"
            "<code>/wd Jimmy88 300 CIMB TERRI</code>"
        )

    player = args[0]
    amount = args[1]
    bank = " ".join(args[2:])

    if not re.match(r"^\d+(\.\d{1,2})?$", amount.replace(",", "")):
        return "❌ Amount tidak valid."

    active, bank_status = is_bank_active(bank)

    if not active:
        return (
            f"❌ <b>WD REJECTED</b>\n\n"
            f"👤 Player: <b>{player}</b>\n"
            f"🏦 WD Bank: <b>{bank}</b>\n"
            f"Status: <b>{bank_status}</b>\n\n"
            f"Reason: Bank is STOP/LIMIT/ISSUE or not found in Bank_List."
        )

    used, summary_status = bank_was_used_for_deposit(player, bank)

    if summary_status is None:
        return (
            f"❌ Bank not found in Player_Summary header:\n"
            f"<b>{bank}</b>\n\n"
            f"Pastikan nama bank sama persis dengan header di Player_Summary."
        )

    if used:
        return (
            f"❌ <b>WD REJECTED</b>\n\n"
            f"👤 Player: <b>{player}</b>\n"
            f"🏦 WD Bank: <b>{bank}</b>\n\n"
            f"Reason: Player already used this bank for deposit."
        )

    amount = amount.replace(",", "")

    append_transaction(
        player=player,
        tx_type="Withdraw",
        amount=amount,
        bank=bank,
        entered_by=username,
        remark="Telegram Bot Withdraw",
    )

    return (
        f"✅ <b>WD allowed & recorded</b>\n\n"
        f"👤 Player: <b>{player}</b>\n"
        f"💰 Amount: <b>{amount}</b>\n"
        f"🏦 WD Bank: <b>{bank}</b>"
    )


def list_active_banks():
    bank_map = get_bank_status_map()

    active = []
    stopped = []

    for item in bank_map.values():
        name = item["name"]
        status = item["status"]

        if status in ACTIVE_STATUS:
            active.append(name)
        else:
            stopped.append(f"{name} ({status})")

    active_text = "\n".join(active) if active else "-"
    stopped_text = "\n".join(stopped) if stopped else "-"

    return (
        f"🏦 <b>Bank Status</b>\n\n"
        f"✅ ACTIVE:\n{active_text}\n\n"
        f"🚫 STOP/LIMIT/ISSUE:\n{stopped_text}"
    )


@app.route("/", methods=["GET"])
def home():
    return "Bank bot running."


@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(silent=True) or {}

    message = data.get("message") or data.get("edited_message")
    if not message:
        return "ok"

    chat_id = message["chat"]["id"]
    text = message.get("text", "")
    msg_id = message.get("message_id")
    username = (
        message.get("from", {}).get("username")
        or message.get("from", {}).get("first_name")
        or "BOT"
    )

    command, args = parse_command(text)

    try:
        if command in ["/start", "/help"]:
            reply = (
                "🤖 <b>Bank WD Checker Bot</b>\n\n"
                "Commands:\n"
                "<code>/check player</code>\n"
                "<code>/dep player amount bank</code>\n"
                "<code>/wd player amount bank</code>\n"
                "<code>/banks</code>\n\n"
                "Example:\n"
                "<code>/check Jimmy88</code>\n"
                "<code>/dep Jimmy88 500 ANEXT HORIZON</code>\n"
                "<code>/wd Jimmy88 300 CIMB TERRI</code>"
            )

        elif command == "/check":
            if not args:
                reply = "❌ Format: <code>/check player</code>"
            else:
                reply = check_player(" ".join(args))

        elif command == "/dep":
            reply = handle_dep(args, username)

        elif command == "/wd":
            reply = handle_wd(args, username)

        elif command == "/banks":
            reply = list_active_banks()

        else:
            return "ok"

    except Exception as e:
        reply = f"❌ Bot error:\n<code>{str(e)}</code>"

    send_message(chat_id, reply, msg_id)
    return "ok"


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
