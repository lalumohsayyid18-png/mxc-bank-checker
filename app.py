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

# Per member + per bank + per day
MAX_DAILY_DEPOSIT_PER_BANK = int(os.environ.get("MAX_DAILY_DEPOSIT_PER_BANK", "3"))

app = Flask(__name__)


def clean_text(x):
    return str(x or "").strip()


def normalize(x):
    return clean_text(x).lower()


def today_str():
    return datetime.now(ZoneInfo(TIMEZONE)).strftime("%Y-%m-%d")


def now_str():
    return datetime.now(ZoneInfo(TIMEZONE)).strftime("%Y-%m-%d %H:%M:%S")


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


def safe_get(row, index, default=""):
    try:
        return clean_text(row[index])
    except Exception:
        return default


def get_bank_status_map():
    """
    Bank_List expected:
    Row 3 header:
    A = Bank Name
    B = Alias
    C = Status

    Data starts row 4.
    """
    ss = get_sheet()
    ws = ss.worksheet(SHEET_BANK_LIST)
    rows = ws.get_all_values()

    bank_map = {}
    alias_map = {}

    for row in rows[3:]:
        bank_name = safe_get(row, 0)
        alias = safe_get(row, 1)
        status = safe_get(row, 2).upper() or "ACTIVE"

        if not bank_name:
            continue

        bank_map[normalize(bank_name)] = {
            "name": bank_name,
            "alias": alias,
            "status": status,
        }

        if alias:
            alias_map[normalize(alias)] = bank_name

        # also allow full bank name as alias
        alias_map[normalize(bank_name)] = bank_name

    return bank_map, alias_map


def resolve_bank(bank_input):
    """
    Convert alias/full text to official bank name from Bank_List.
    Example:
    award -> CIMB AWARD CLOTHING
    """
    bank_map, alias_map = get_bank_status_map()
    key = normalize(bank_input)

    # exact alias/full match
    if key in alias_map:
        bank_name = alias_map[key]
        item = bank_map.get(normalize(bank_name))
        status = item["status"] if item else "NOT FOUND"
        return bank_name, status

    # partial matching, e.g. "awar" can find "award" if unique
    matches = []
    for alias_key, bank_name in alias_map.items():
        if key and key in alias_key:
            matches.append(bank_name)

    unique_matches = []
    seen = set()
    for m in matches:
        mk = normalize(m)
        if mk not in seen:
            seen.add(mk)
            unique_matches.append(m)

    if len(unique_matches) == 1:
        bank_name = unique_matches[0]
        item = bank_map.get(normalize(bank_name))
        status = item["status"] if item else "NOT FOUND"
        return bank_name, status

    if len(unique_matches) > 1:
        return None, "AMBIGUOUS"

    return None, "NOT FOUND"


def is_bank_active_by_status(status):
    return clean_text(status).upper() in ACTIVE_STATUS


def get_all_active_banks():
    bank_map, _ = get_bank_status_map()
    active = []
    stopped = []

    for item in bank_map.values():
        name = item["name"]
        status = item["status"]

        if is_bank_active_by_status(status):
            active.append(name)
        else:
            stopped.append(f"{name} ({status})")

    return active, stopped


def find_summary_row(player):
    ss = get_sheet()
    ws = ss.worksheet(SHEET_PLAYER_SUMMARY)
    rows = ws.get_all_values()

    if not rows or len(rows) < 4:
        return None, None

    headers = rows[2]
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


def get_transactions_rows():
    ss = get_sheet()
    ws = ss.worksheet(SHEET_TRANSACTIONS)
    return ws.get_all_values()


def parse_date_is_today(value):
    """
    Accepts:
    2026-05-12 18:22:01
    2026-05-12
    12/05/2026
    5/12/2026
    Google text date formats will still pass if starts with YYYY-MM-DD.
    """
    raw = clean_text(value)
    if not raw:
        return False

    today = today_str()

    if raw.startswith(today):
        return True

    # Try common date formats
    fmts = [
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
        "%d/%m/%Y",
        "%d/%m/%Y %H:%M:%S",
        "%m/%d/%Y",
        "%m/%d/%Y %H:%M:%S",
    ]

    for fmt in fmts:
        try:
            dt = datetime.strptime(raw, fmt)
            return dt.strftime("%Y-%m-%d") == today
        except Exception:
            pass

    return False


def get_today_deposit_counts_by_bank(player):
    """
    Count per member + per bank + per day from Transactions:
    A Date | B Player | C Type | D Bank | E Amount | F Remark | G Entered By
    """
    rows = get_transactions_rows()
    player_key = normalize(player)
    counts = {}

    for row in rows[3:]:
        date_value = safe_get(row, 0)
        tx_player = safe_get(row, 1)
        tx_type = safe_get(row, 2)
        bank = safe_get(row, 3)

        if not bank:
            continue

        if normalize(tx_player) != player_key:
            continue

        if normalize(tx_type) != "deposit":
            continue

        if not parse_date_is_today(date_value):
            continue

        bank_key = normalize(bank)
        if bank_key not in counts:
            counts[bank_key] = {
                "bank": bank,
                "count": 0,
            }

        counts[bank_key]["count"] += 1

    return counts


def get_used_deposit_banks_from_summary(headers, row):
    used = get_summary_value(headers, row, "Deposit Banks Used")
    return split_bank_list(used)


def get_player_check_message(player):
    headers, row = find_summary_row(player)
    if not row:
        return f"❌ Player not found:\n<b>{player}</b>"

    official_player = clean_text(row[0])
    used_banks = get_used_deposit_banks_from_summary(headers, row)
    today_counts = get_today_deposit_counts_by_bank(official_player)
    active_banks, stopped_banks = get_all_active_banks()

    # Deposit banks used, filtered only simple output
    used_text = "\n".join([f"• {b}" for b in used_banks]) if used_banks else "-"

    # Today usage only for banks this player has used today
    today_lines = []
    danger_lines = []

    for item in today_counts.values():
        bank = item["bank"]
        count = item["count"]
        today_lines.append(f"• {bank}: {count}x today")

        if count >= MAX_DAILY_DEPOSIT_PER_BANK:
            danger_lines.append(f"🚫 DO NOT GIVE {bank} ({count}/{MAX_DAILY_DEPOSIT_PER_BANK})")
        elif count == MAX_DAILY_DEPOSIT_PER_BANK - 1:
            danger_lines.append(f"⚠️ Almost limit {bank} ({count}/{MAX_DAILY_DEPOSIT_PER_BANK})")

    today_text = "\n".join(today_lines) if today_lines else "-"

    # Show stopped banks only
    stop_text = "\n".join([f"• {x}" for x in stopped_banks]) if stopped_banks else "-"

    warning_text = "\n".join(danger_lines) if danger_lines else "OK"

    return (
        f"🔎 <b>Player Check</b>\n\n"
        f"👤 Player: <b>{official_player}</b>\n\n"
        f"🏦 Deposit Banks Used:\n{used_text}\n\n"
        f"📊 Today Usage:\n{today_text}\n\n"
        f"⛔ STOP/LIMIT/INACTIVE Banks:\n{stop_text}\n\n"
        f"⚠️ Warning:\n{warning_text}"
    )


def append_transaction(player, tx_type, amount, bank, entered_by="BOT", remark=""):
    ss = get_sheet()
    ws = ss.worksheet(SHEET_TRANSACTIONS)

    # A Date | B Player | C Type | D Bank | E Amount | F Remark | G Entered By
    ws.append_row(
        [now_str(), player, tx_type, bank, amount, remark, entered_by],
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


def get_player_from_replied_message(message):
    reply = message.get("reply_to_message") or {}
    text = clean_text(reply.get("text") or reply.get("caption") or "")

    if not text:
        return ""

    # Use first line as player name
    first_line = clean_text(text.splitlines()[0])

    # If CS message has common prefix like "Player: ani0128"
    m = re.search(r"(?:player|username|user)\s*[:：]\s*(.+)", first_line, re.I)
    if m:
        return clean_text(m.group(1))

    return first_line


def parse_reply_deposit(text):
    """
    Accept:
    +500 award
    +1,000 award
    +300.50 horizon
    """
    raw = clean_text(text)

    m = re.match(r"^\+(\d[\d,]*(?:\.\d{1,2})?)\s+(.+)$", raw)
    if not m:
        return None

    amount = m.group(1).replace(",", "")
    bank_alias = clean_text(m.group(2))

    return amount, bank_alias


def handle_reply_deposit(message):
    text = clean_text(message.get("text", ""))
    parsed = parse_reply_deposit(text)

    if not parsed:
        return None

    amount, bank_alias = parsed
    player = get_player_from_replied_message(message)

    if not player:
        return (
            "❌ Please reply to CS/player message when confirming deposit.\n\n"
            "Format:\n"
            "<code>+500 award</code>"
        )

    bank_name, bank_status = resolve_bank(bank_alias)

    if bank_status == "AMBIGUOUS":
        return (
            f"❌ Bank alias ambiguous:\n"
            f"<b>{bank_alias}</b>\n\n"
            f"Please use clearer alias."
        )

    if not bank_name:
        return (
            f"❌ Bank alias not found:\n"
            f"<b>{bank_alias}</b>\n\n"
            f"Please check Bank_List alias."
        )

    if not is_bank_active_by_status(bank_status):
        return (
            f"❌ <b>DEPOSIT REJECTED</b>\n\n"
            f"👤 Player: <b>{player}</b>\n"
            f"🏦 Bank: <b>{bank_name}</b>\n"
            f"Status: <b>{bank_status}</b>\n\n"
            f"Reason: Bank is not ACTIVE."
        )

    # Check today's usage BEFORE recording this deposit
    today_counts = get_today_deposit_counts_by_bank(player)
    current_count = today_counts.get(normalize(bank_name), {}).get("count", 0)

    if current_count >= MAX_DAILY_DEPOSIT_PER_BANK:
        return (
            f"🚫 <b>DEPOSIT REJECTED</b>\n\n"
            f"👤 Player: <b>{player}</b>\n"
            f"🏦 Bank: <b>{bank_name}</b>\n"
            f"Today usage: <b>{current_count}/{MAX_DAILY_DEPOSIT_PER_BANK}</b>\n\n"
            f"Reason: Player already reached daily limit for this bank."
        )

    username = (
        message.get("from", {}).get("username")
        or message.get("from", {}).get("first_name")
        or "BOT"
    )

    append_transaction(
        player=player,
        tx_type="Deposit",
        amount=amount,
        bank=bank_name,
        entered_by=username,
        remark=f"Telegram reply + | alias: {bank_alias}",
    )

    new_count = current_count + 1

    warning = "OK"
    if new_count >= MAX_DAILY_DEPOSIT_PER_BANK:
        warning = f"🚫 LIMIT REACHED for {bank_name} ({new_count}/{MAX_DAILY_DEPOSIT_PER_BANK})"
    elif new_count == MAX_DAILY_DEPOSIT_PER_BANK - 1:
        warning = f"⚠️ Almost limit for {bank_name} ({new_count}/{MAX_DAILY_DEPOSIT_PER_BANK})"

    return (
        f"✅ <b>Deposit recorded</b>\n\n"
        f"👤 Player: <b>{player}</b>\n"
        f"💰 Amount: <b>{amount}</b>\n"
        f"🏦 Bank: <b>{bank_name}</b>\n"
        f"📊 Today usage: <b>{new_count}/{MAX_DAILY_DEPOSIT_PER_BANK}</b>\n\n"
        f"{warning}"
    )


def handle_dep_command(args, username):
    if len(args) < 3:
        return (
            "❌ Format salah.\n\n"
            "Gunakan:\n"
            "<code>/dep player amount bank_alias</code>\n\n"
            "Contoh:\n"
            "<code>/dep Jimmy88 500 horizon</code>"
        )

    player = args[0]
    amount = args[1].replace(",", "")
    bank_alias = " ".join(args[2:])

    if not re.match(r"^\d+(\.\d{1,2})?$", amount):
        return "❌ Amount tidak valid."

    bank_name, bank_status = resolve_bank(bank_alias)

    if bank_status == "AMBIGUOUS":
        return f"❌ Bank alias ambiguous: <b>{bank_alias}</b>"

    if not bank_name:
        return f"❌ Bank alias not found: <b>{bank_alias}</b>"

    if not is_bank_active_by_status(bank_status):
        return (
            f"❌ <b>DEPOSIT REJECTED</b>\n\n"
            f"🏦 Bank: <b>{bank_name}</b>\n"
            f"Status: <b>{bank_status}</b>\n\n"
            f"Reason: Bank is not ACTIVE in Bank_List."
        )

    today_counts = get_today_deposit_counts_by_bank(player)
    current_count = today_counts.get(normalize(bank_name), {}).get("count", 0)

    if current_count >= MAX_DAILY_DEPOSIT_PER_BANK:
        return (
            f"🚫 <b>DEPOSIT REJECTED</b>\n\n"
            f"👤 Player: <b>{player}</b>\n"
            f"🏦 Bank: <b>{bank_name}</b>\n"
            f"Today usage: <b>{current_count}/{MAX_DAILY_DEPOSIT_PER_BANK}</b>"
        )

    append_transaction(
        player=player,
        tx_type="Deposit",
        amount=amount,
        bank=bank_name,
        entered_by=username,
        remark=f"Telegram /dep | alias: {bank_alias}",
    )

    new_count = current_count + 1

    return (
        f"✅ <b>Deposit recorded</b>\n\n"
        f"👤 Player: <b>{player}</b>\n"
        f"💰 Amount: <b>{amount}</b>\n"
        f"🏦 Bank: <b>{bank_name}</b>\n"
        f"📊 Today usage: <b>{new_count}/{MAX_DAILY_DEPOSIT_PER_BANK}</b>"
    )


def handle_wd(args, username):
    if len(args) < 3:
        return (
            "❌ Format salah.\n\n"
            "Gunakan:\n"
            "<code>/wd player amount bank_alias</code>\n\n"
            "Contoh:\n"
            "<code>/wd Jimmy88 300 cozy</code>"
        )

    player = args[0]
    amount = args[1].replace(",", "")
    bank_alias = " ".join(args[2:])

    if not re.match(r"^\d+(\.\d{1,2})?$", amount):
        return "❌ Amount tidak valid."

    bank_name, bank_status = resolve_bank(bank_alias)

    if bank_status == "AMBIGUOUS":
        return f"❌ Bank alias ambiguous: <b>{bank_alias}</b>"

    if not bank_name:
        return f"❌ Bank alias not found: <b>{bank_alias}</b>"

    if not is_bank_active_by_status(bank_status):
        return (
            f"❌ <b>WD REJECTED</b>\n\n"
            f"👤 Player: <b>{player}</b>\n"
            f"🏦 WD Bank: <b>{bank_name}</b>\n"
            f"Status: <b>{bank_status}</b>\n\n"
            f"Reason: Bank is STOP/LIMIT/ISSUE or not found in Bank_List."
        )

    used, summary_status = bank_was_used_for_deposit(player, bank_name)

    if summary_status is None:
        return (
            f"❌ Bank not found in Player_Summary header:\n"
            f"<b>{bank_name}</b>\n\n"
            f"Pastikan nama bank sama persis dengan header di Player_Summary."
        )

    if used:
        return (
            f"❌ <b>WD REJECTED</b>\n\n"
            f"👤 Player: <b>{player}</b>\n"
            f"🏦 WD Bank: <b>{bank_name}</b>\n\n"
            f"Reason: Player already used this bank for deposit."
        )

    append_transaction(
        player=player,
        tx_type="Withdraw",
        amount=amount,
        bank=bank_name,
        entered_by=username,
        remark=f"Telegram Bot Withdraw | alias: {bank_alias}",
    )

    return (
        f"✅ <b>WD allowed & recorded</b>\n\n"
        f"👤 Player: <b>{player}</b>\n"
        f"💰 Amount: <b>{amount}</b>\n"
        f"🏦 WD Bank: <b>{bank_name}</b>"
    )


def list_active_banks():
    active, stopped = get_all_active_banks()

    active_text = "\n".join([f"• {x}" for x in active]) if active else "-"
    stopped_text = "\n".join([f"• {x}" for x in stopped]) if stopped else "-"

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
    text = clean_text(message.get("text", ""))
    msg_id = message.get("message_id")
    username = (
        message.get("from", {}).get("username")
        or message.get("from", {}).get("first_name")
        or "BOT"
    )

    try:
        # Reply confirmation: +500 award
        if text.startswith("+"):
            reply = handle_reply_deposit(message)
            if reply:
                send_message(chat_id, reply, msg_id)
            return "ok"

        command, args = parse_command(text)

        if command in ["/start", "/help"]:
            reply = (
                "🤖 <b>Bank Deposit Checker Bot</b>\n\n"
                "Commands:\n"
                "<code>/check player</code>\n"
                "<code>/banks</code>\n"
                "<code>/dep player amount bank_alias</code>\n"
                "<code>/wd player amount bank_alias</code>\n\n"
                "Reply confirm deposit:\n"
                "<code>+500 award</code>\n\n"
                "Examples:\n"
                "<code>/check ani0128</code>\n"
                "<code>/dep Jimmy88 500 horizon</code>\n"
                "<code>/wd Jimmy88 300 cozy</code>"
            )

        elif command == "/check":
            if not args:
                reply = "❌ Format: <code>/check player</code>"
            else:
                reply = get_player_check_message(" ".join(args))

        elif command == "/banks":
            reply = list_active_banks()

        elif command == "/dep":
            reply = handle_dep_command(args, username)

        elif command == "/wd":
            reply = handle_wd(args, username)

        else:
            return "ok"

    except Exception as e:
        reply = f"❌ Bot error:\n<code>{str(e)}</code>"

    send_message(chat_id, reply, msg_id)
    return "ok"


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
