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

MAX_DAILY_DEPOSIT_PER_BANK = 3
ACTIVE_STATUS = {"ACTIVE"}

app = Flask(__name__)


def clean_text(x):
    return str(x or "").strip()


def normalize(x):
    return clean_text(x).lower()


def now():
    return datetime.now(ZoneInfo(TIMEZONE))


def today_str():
    return now().strftime("%Y-%m-%d")


def now_str():
    return now().strftime("%Y-%m-%d %H:%M:%S")


def parse_amount(x):
    try:
        return float(str(x).replace(",", "").strip() or 0)
    except:
        return 0.0


def fmt_money(x):
    if float(x).is_integer():
        return f"{int(x):,}"
    return f"{x:,.2f}"


def get_sheet():
    creds_dict = json.loads(GOOGLE_CREDENTIALS)

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]

    creds = Credentials.from_service_account_info(
        creds_dict,
        scopes=scopes
    )

    client = gspread.authorize(creds)
    return client.open_by_key(SPREADSHEET_ID)


def send_message(chat_id, text, reply_to_message_id=None):
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML"
    }

    if reply_to_message_id:
        payload["reply_to_message_id"] = reply_to_message_id

    requests.post(
        f"{TELEGRAM_API}/sendMessage",
        json=payload,
        timeout=10
    )


def safe_get(row, index, default=""):
    try:
        return clean_text(row[index])
    except:
        return default


def get_bank_maps():
    ss = get_sheet()
    ws = ss.worksheet(SHEET_BANK_LIST)

    rows = ws.get_all_values()

    bank_map = {}
    alias_map = {}

    for row in rows[3:]:
        bank = safe_get(row, 0)
        alias = safe_get(row, 1)
        status = safe_get(row, 2).upper() or "ACTIVE"

        if not bank:
            continue

        bank_map[normalize(bank)] = {
            "name": bank,
            "alias": alias,
            "status": status
        }

        alias_map[normalize(bank)] = bank

        if alias:
            alias_map[normalize(alias)] = bank

    return bank_map, alias_map


def resolve_bank(bank_input):
    bank_map, alias_map = get_bank_maps()

    key = normalize(bank_input)

    if key in alias_map:
        bank = alias_map[key]
        status = bank_map[normalize(bank)]["status"]
        return bank, status

    return None, "NOT FOUND"


def get_active_and_stopped_banks():
    bank_map, _ = get_bank_maps()

    active = []
    stopped = []

    for item in bank_map.values():
        if item["status"] in ACTIVE_STATUS:
            active.append(item["name"])
        else:
            stopped.append(
                f"{item['name']} ({item['status']})"
            )

    return active, stopped


def parse_date_is_today(value):
    raw = clean_text(value)

    if not raw:
        return False

    if raw.startswith(today_str()):
        return True

    fmts = [
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
        "%d/%m/%Y",
        "%d/%m/%Y %H:%M:%S",
    ]

    for fmt in fmts:
        try:
            dt = datetime.strptime(raw, fmt)
            return dt.strftime("%Y-%m-%d") == today_str()
        except:
            pass

    return False


def get_transactions():
    ss = get_sheet()
    ws = ss.worksheet(SHEET_TRANSACTIONS)
    return ws.get_all_values()


def get_today_stats(player=None):
    rows = get_transactions()

    player_counts = {}
    bank_totals = {}

    player_key = normalize(player) if player else None

    for row in rows[3:]:
        date_value = safe_get(row, 0)
        tx_player = safe_get(row, 1)
        tx_type = safe_get(row, 2)
        bank = safe_get(row, 3)
        amount = parse_amount(safe_get(row, 4))

        if normalize(tx_type) != "deposit":
            continue

        if not bank:
            continue

        if not parse_date_is_today(date_value):
            continue

        bank_key = normalize(bank)

        bank_totals[bank_key] = (
            bank_totals.get(bank_key, 0.0) + amount
        )

        if player_key and normalize(tx_player) == player_key:
            player_counts[bank_key] = (
                player_counts.get(bank_key, 0) + 1
            )

    return player_counts, bank_totals


def find_summary_row(player):
    ss = get_sheet()
    ws = ss.worksheet(SHEET_PLAYER_SUMMARY)

    rows = ws.get_all_values()

    if len(rows) < 4:
        return None, None

    headers = rows[2]

    for row in rows[3:]:
        if normalize(safe_get(row, 0)) == normalize(player):
            return headers, row

    return headers, None


def get_summary_value(headers, row, header_name):
    for i, h in enumerate(headers):
        if normalize(h) == normalize(header_name):
            return safe_get(row, i)

    return ""


def split_bank_list(value):
    if not value or value == "-":
        return []

    return [
        clean_text(x)
        for x in str(value).split(",")
        if clean_text(x)
    ]


def get_used_banks(headers, row):
    return split_bank_list(
        get_summary_value(
            headers,
            row,
            "Deposit Banks Used"
        )
    )


def append_transaction(
    player,
    tx_type,
    amount,
    bank,
    entered_by="BOT",
    remark=""
):
    ss = get_sheet()
    ws = ss.worksheet(SHEET_TRANSACTIONS)

    ws.append_row(
        [
            now_str(),
            player,
            tx_type,
            bank,
            amount,
            remark,
            entered_by
        ],
        value_input_option="USER_ENTERED"
    )


def get_player_check(player):
    headers, row = find_summary_row(player)

    if not row:
        return (
            f"❌ Player not found:\n"
            f"<b>{player}</b>"
        )

    official_player = safe_get(row, 0)

    used_banks = get_used_banks(headers, row)

    active_banks, stopped_banks = (
        get_active_and_stopped_banks()
    )

    active_set = {
        normalize(x)
        for x in active_banks
    }

    player_counts, bank_totals = (
        get_today_stats(official_player)
    )

    if not used_banks:
        deposit_bank_text = "-"
    else:
        lines = []

        for bank in used_banks:
            key = normalize(bank)

            player_count = player_counts.get(key, 0)

            bank_total = bank_totals.get(
                key,
                0.0
            )

            if key not in active_set:
                lines.append(
                    f"• {bank} — NOT ACTIVE"
                )
            else:
                lines.append(
                    f"• {bank} — "
                    f"player {player_count}/{MAX_DAILY_DEPOSIT_PER_BANK} | "
                    f"bank today {fmt_money(bank_total)}"
                )

        deposit_bank_text = "\n".join(lines)

    stop_lines = [
        f"• {x}"
        for x in stopped_banks
    ]

    stop_text = (
        "\n".join(stop_lines)
        if stop_lines else "-"
    )

    return (
        f"🔎 <b>Player Check</b>\n\n"
        f"👤 Player: <b>{official_player}</b>\n\n"
        f"🏦 Player Deposit Banks:\n"
        f"{deposit_bank_text}\n\n"
        f"🚫 Do Not Give:\n"
        f"{stop_text}"
    )


def handle_dep(args, username):
    if len(args) < 3:
        return (
            "❌ Format:\n"
            "<code>/dep player amount bank_alias</code>"
        )

    player = args[0]
    amount = args[1].replace(",", "")
    bank_alias = " ".join(args[2:])

    if not re.match(r"^\d+(\.\d{1,2})?$", amount):
        return "❌ Amount tidak valid."

    bank, status = resolve_bank(bank_alias)

    if not bank:
        return (
            f"❌ Bank alias not found:\n"
            f"<b>{bank_alias}</b>"
        )

    if status not in ACTIVE_STATUS:
        return (
            f"❌ DEPOSIT REJECTED\n\n"
            f"🏦 Bank: <b>{bank}</b>\n"
            f"Status: <b>{status}</b>"
        )

    player_counts, bank_totals = (
        get_today_stats(player)
    )

    current_count = player_counts.get(
        normalize(bank),
        0
    )

    if current_count >= MAX_DAILY_DEPOSIT_PER_BANK:
        return (
            f"🚫 DEPOSIT REJECTED\n\n"
            f"👤 Player: <b>{player}</b>\n"
            f"🏦 Bank: <b>{bank}</b>\n"
            f"Player usage today: "
            f"<b>{current_count}/{MAX_DAILY_DEPOSIT_PER_BANK}</b>"
        )

    append_transaction(
        player=player,
        tx_type="Deposit",
        amount=amount,
        bank=bank,
        entered_by=username,
        remark=f"Telegram /dep | alias: {bank_alias}"
    )

    amount_float = parse_amount(amount)

    new_count = current_count + 1

    new_bank_total = (
        bank_totals.get(normalize(bank), 0.0)
        + amount_float
    )

    return (
        f"✅ <b>Deposit recorded</b>\n\n"
        f"👤 Player: <b>{player}</b>\n"
        f"💰 Amount: <b>{fmt_money(amount_float)}</b>\n"
        f"🏦 Bank: <b>{bank}</b>\n"
        f"📊 Player usage today: "
        f"<b>{new_count}/{MAX_DAILY_DEPOSIT_PER_BANK}</b>\n"
        f"🏦 Bank total today: "
        f"<b>{fmt_money(new_bank_total)}</b>"
    )


def handle_wd(args, username):
    if len(args) < 3:
        return (
            "❌ Format:\n"
            "<code>/wd player amount bank_alias</code>"
        )

    player = args[0]
    amount = args[1].replace(",", "")
    bank_alias = " ".join(args[2:])

    bank, status = resolve_bank(bank_alias)

    if not bank:
        return (
            f"❌ Bank alias not found:\n"
            f"<b>{bank_alias}</b>"
        )

    append_transaction(
        player=player,
        tx_type="Withdraw",
        amount=amount,
        bank=bank,
        entered_by=username,
        remark=f"Telegram /wd | alias: {bank_alias}"
    )

    return (
        f"✅ WD recorded\n"
        f"👤 {player}\n"
        f"💰 {amount}\n"
        f"🏦 {bank}"
    )


def list_banks():
    active, stopped = (
        get_active_and_stopped_banks()
    )

    _, bank_totals = get_today_stats()

    active_lines = []

    for bank in active:
        active_lines.append(
            f"• {bank} — "
            f"{fmt_money(bank_totals.get(normalize(bank), 0.0))}"
        )

    stopped_lines = [
        f"• {x}"
        for x in stopped
    ]

    return (
        f"🏦 <b>Bank Status</b>\n\n"
        f"✅ ACTIVE:\n"
        f"{chr(10).join(active_lines) if active_lines else '-'}\n\n"
        f"🚫 STOP/LIMIT/ISSUE:\n"
        f"{chr(10).join(stopped_lines) if stopped_lines else '-'}"
    )


@app.route("/", methods=["GET"])
def home():
    return "Bank bot running."


@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(silent=True) or {}

    message = (
        data.get("message")
        or data.get("edited_message")
    )

    if not message:
        return "ok"

    chat_id = message["chat"]["id"]

    text = clean_text(
        message.get("text", "")
    )

    msg_id = message.get("message_id")

    username = (
        message.get("from", {}).get("username")
        or message.get("from", {}).get("first_name")
        or "BOT"
    )

    try:
        parts = text.split()

        command = (
            parts[0].lower()
            if parts else ""
        )

        args = parts[1:]

        if command in ["/start", "/help"]:
            reply = (
                "🤖 <b>BANK ASSISTANCE</b>\n\n"
                "<code>/check player</code>\n"
                "<code>/dep player amount bank_alias</code>\n"
                "<code>/wd player amount bank_alias</code>\n"
                "<code>/banks</code>"
            )

        elif command == "/check":
            if not args:
                reply = (
                    "❌ Format:\n"
                    "<code>/check player</code>"
                )
            else:
                reply = get_player_check(
                    " ".join(args)
                )

        elif command == "/dep":
            reply = handle_dep(args, username)

        elif command == "/wd":
            reply = handle_wd(args, username)

        elif command == "/banks":
            reply = list_banks()

        else:
            return "ok"

    except Exception as e:
        reply = (
            f"❌ Bot error:\n"
            f"<code>{str(e)}</code>"
        )

    send_message(
        chat_id,
        reply,
        msg_id
    )

    return "ok"


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 10000))
    )
