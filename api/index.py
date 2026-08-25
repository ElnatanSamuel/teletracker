import datetime
import logging
import os
from dotenv import load_dotenv
from fastapi import FastAPI, Request, Response
import httpx

load_dotenv()

logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.getenv("BOT_TOKEN")
RAW_DB_URL = os.getenv("TURSO_DATABASE_URL", "").strip()
TURSO_AUTH_TOKEN = os.getenv("TURSO_AUTH_TOKEN", "").strip()

if RAW_DB_URL.startswith("libsql://"):
    TURSO_DB_URL = RAW_DB_URL.replace("libsql://", "https://")
elif not RAW_DB_URL.startswith("http://") and not RAW_DB_URL.startswith("https://"):
    TURSO_DB_URL = f"https://{RAW_DB_URL}"
else:
    TURSO_DB_URL = RAW_DB_URL

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

app = FastAPI()


# --- Direct Turso HTTP Helper ---

async def query_turso(sql: str, args: list = None):
    args = args or []
    formatted_args = []
    for a in args:
        if a is None:
            formatted_args.append({"type": "null"})
        elif isinstance(a, bool):
            formatted_args.append({"type": "integer", "value": 1 if a else 0})
        elif isinstance(a, int):
            formatted_args.append({"type": "integer", "value": str(a)})
        elif isinstance(a, float):
            formatted_args.append({"type": "float", "value": a})
        else:
            formatted_args.append({"type": "text", "value": str(a)})

    payload = {
        "requests": [
            {
                "type": "execute",
                "stmt": {
                    "sql": sql,
                    "args": formatted_args
                }
            },
            {"type": "close"}
        ]
    }

    url = f"{TURSO_DB_URL}/v2/pipeline"
    headers = {
        "Authorization": f"Bearer {TURSO_AUTH_TOKEN}",
        "Content-Type": "application/json"
    }

    async with httpx.AsyncClient(timeout=10.0) as client:
        res = await client.post(url, json=payload, headers=headers)
        res_data = res.json()
        if "results" in res_data and len(res_data["results"]) > 0:
            result_obj = res_data["results"][0]
            if result_obj.get("type") == "error":
                raise RuntimeError(f"Turso Error: {result_obj.get('error')}")
            response = result_obj.get("response", {}).get("result", {})
            rows = []
            for r in response.get("rows", []):
                rows.append([col.get("value") if isinstance(col, dict) else col for col in r])
            return rows
        return []


async def init_db():
    await query_turso(
        """
        CREATE TABLE IF NOT EXISTS accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            bank_name TEXT,
            balance REAL,
            UNIQUE(user_id, bank_name)
        )
        """
    )
    await query_turso(
        """
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            bank_name TEXT,
            type TEXT,
            amount REAL,
            description TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    await query_turso(
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            chat_id INTEGER,
            state TEXT,
            state_data TEXT
        )
        """
    )


# --- Direct Telegram Helpers (HTML Mode) ---

async def send_tg_message(chat_id: int, text: str, reply_markup: dict = None):
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    async with httpx.AsyncClient(timeout=10.0) as client:
        res = await client.post(f"{TELEGRAM_API}/sendMessage", json=payload)
        if res.status_code != 200:
            logging.error(f"Telegram sendMessage error: {res.status_code} - {res.text}")


async def edit_tg_message(chat_id: int, message_id: int, text: str, reply_markup: dict = None):
    payload = {"chat_id": chat_id, "message_id": message_id, "text": text, "parse_mode": "HTML"}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    async with httpx.AsyncClient(timeout=10.0) as client:
        res = await client.post(f"{TELEGRAM_API}/editMessageText", json=payload)
        if res.status_code != 200:
            logging.error(f"Telegram editMessageText error: {res.status_code} - {res.text}")


async def answer_tg_callback(callback_query_id: str):
    async with httpx.AsyncClient(timeout=10.0) as client:
        await client.post(f"{TELEGRAM_API}/answerCallbackQuery", json={"callback_query_id": callback_query_id})


# --- Keyboards ---

def main_menu_keyboard():
    return {
        "inline_keyboard": [
            [
                {"text": "💸 Spend", "callback_data": "btn_spend"},
                {"text": "💰 Income", "callback_data": "btn_income"}
            ],
            [
                {"text": "🏦 Add / Edit Bank", "callback_data": "btn_addbank"},
                {"text": "📊 Balances", "callback_data": "btn_balance"}
            ],
            [
                {"text": "📈 Today's Summary", "callback_data": "btn_summary"}
            ]
        ]
    }


def cancel_keyboard():
    return {"inline_keyboard": [[{"text": "❌ Cancel", "callback_data": "btn_cancel"}]]}


async def get_bank_selection_keyboard(user_id: int, action_prefix: str):
    banks = await query_turso("SELECT bank_name, balance FROM accounts WHERE user_id = ?", [user_id])
    if not banks:
        return None
    buttons = []
    for row in banks:
        name = row[0]
        bal = float(row[1]) if row[1] is not None else 0.0
        buttons.append([{"text": f"{name} (${bal:,.2f})", "callback_data": f"{action_prefix}:{name}"}])
    buttons.append([{"text": "❌ Cancel", "callback_data": "btn_cancel"}])
    return {"inline_keyboard": buttons}


# --- Reports ---

async def get_balances_text(user_id: int) -> str:
    rows = await query_turso("SELECT bank_name, balance FROM accounts WHERE user_id = ?", [user_id])
    if not rows:
        return "🏦 <b>No accounts found.</b> Tap 'Add / Edit Bank' to add one."
    total = sum(float(r[1]) for r in rows if r[1] is not None)
    lines = [f"• <b>{r[0]}</b>: ${float(r[1]):,.2f}" for r in rows if r[1] is not None]
    return "🏦 <b>Current Balances:</b>\n\n" + "\n".join(lines) + f"\n\n<b>Total Net Worth:</b> <code>${total:,.2f}</code>"


async def build_daily_summary(user_id: int) -> str:
    today_str = datetime.date.today().isoformat()
    txs = await query_turso(
        "SELECT bank_name, type, amount, description FROM transactions WHERE user_id = ? AND date(timestamp) = date('now') ORDER BY timestamp ASC",
        [user_id]
    )
    accounts = await query_turso("SELECT bank_name, balance FROM accounts WHERE user_id = ?", [user_id])

    spent = sum(float(t[2]) for t in txs if t[1] == "expense" and t[2] is not None)
    earned = sum(float(t[2]) for t in txs if t[1] == "income" and t[2] is not None)
    net = earned - spent
    total_balance = sum(float(a[1]) for a in accounts if a[1] is not None)

    msg = f"📊 <b>Daily Summary ({today_str})</b>\n\n• <b>Spent Today:</b> -${spent:,.2f}\n• <b>Income Today:</b> +${earned:,.2f}\n• <b>Net Change:</b> {'+$' if net >= 0 else '-$'}{abs(net):,.2f}\n\n"
    if txs:
        msg += "<b>Today's Transactions:</b>\n"
        for bank, tx_type, amt, desc in txs:
            sign = "-" if tx_type == "expense" else "+"
            msg += f"  • <code>{sign}${float(amt):,.2f}</code> ({bank}) - {desc}\n"
        msg += "\n"
    else:
        msg += "<i>No transactions recorded today.</i>\n\n"

    msg += "<b>Balances:</b>\n"
    for bank, bal in accounts:
        msg += f"  • {bank}: ${float(bal):,.2f}\n"
    msg += f"<b>Total Net Worth:</b> <code>${total_balance:,.2f}</code>"
    return msg


# --- Webhook Endpoint ---

@app.post("/api/webhook")
@app.post("/webhook")
async def webhook(request: Request):
    try:
        await init_db()
        data = await request.json()

        # 1. Text Message Received
        if "message" in data:
            msg = data["message"]
            user_id = msg["from"]["id"]
            chat_id = msg["chat"]["id"]
            text = msg.get("text", "").strip()

            if text.startswith("/start"):
                await query_turso(
                    "INSERT INTO users (user_id, chat_id, state, state_data) VALUES (?, ?, NULL, NULL) "
                    "ON CONFLICT(user_id) DO UPDATE SET chat_id = excluded.chat_id, state = NULL, state_data = NULL",
                    [user_id, chat_id]
                )
                await send_tg_message(chat_id, "👋 <b>Welcome to Money Tracker!</b>\n\nTap an option below to get started:", main_menu_keyboard())
                return Response(status_code=200)

            # Check User State in Database
            rows = await query_turso("SELECT state, state_data FROM users WHERE user_id = ?", [user_id])
            user_row = rows[0] if rows else None

            if not user_row or not user_row[0]:
                await send_tg_message(chat_id, "Please select an option from the menu:", main_menu_keyboard())
                return Response(status_code=200)

            state, state_data = user_row[0], user_row[1]

            if state == "AWAITING_ADD_BANK":
                parts = text.split()
                if len(parts) < 2:
                    await send_tg_message(chat_id, "❌ Send: <code>&lt;BankName&gt; &lt;Balance&gt;</code> (e.g. <code>CBE 5000</code>)", cancel_keyboard())
                    return Response(status_code=200)
                bank_name = parts[0].capitalize()
                try:
                    balance = float(parts[1])
                except ValueError:
                    await send_tg_message(chat_id, "❌ Please enter a valid number.", cancel_keyboard())
                    return Response(status_code=200)

                await query_turso(
                    "INSERT INTO accounts (user_id, bank_name, balance) VALUES (?, ?, ?) "
                    "ON CONFLICT(user_id, bank_name) DO UPDATE SET balance = excluded.balance",
                    [user_id, bank_name, balance]
                )
                await query_turso("UPDATE users SET state = NULL, state_data = NULL WHERE user_id = ?", [user_id])
                await send_tg_message(chat_id, f"✅ Account <b>{bank_name}</b> set to <b>${balance:,.2f}</b>.", main_menu_keyboard())

            elif state == "AWAITING_SPEND":
                bank_name = state_data
                parts = text.split(maxsplit=1)
                try:
                    amount = float(parts[0])
                    if amount <= 0:
                        raise ValueError
                except (ValueError, IndexError):
                    await send_tg_message(chat_id, "❌ Enter a valid positive amount and reason.", cancel_keyboard())
                    return Response(status_code=200)

                reason = parts[1] if len(parts) > 1 else "Unspecified"
                accs = await query_turso("SELECT balance FROM accounts WHERE user_id = ? AND bank_name = ?", [user_id, bank_name])
                if not accs:
                    await send_tg_message(chat_id, "Account not found.", main_menu_keyboard())
                    return Response(status_code=200)

                new_balance = float(accs[0][0]) - amount
                await query_turso("UPDATE accounts SET balance = ? WHERE user_id = ? AND bank_name = ?", [new_balance, user_id, bank_name])
                await query_turso(
                    "INSERT INTO transactions (user_id, bank_name, type, amount, description) VALUES (?, ?, 'expense', ?, ?)",
                    [user_id, bank_name, amount, reason]
                )
                await query_turso("UPDATE users SET state = NULL, state_data = NULL WHERE user_id = ?", [user_id])
                await send_tg_message(
                    chat_id,
                    f"💸 <b>Expense Logged!</b>\n• Account: <b>{bank_name}</b>\n• Amount: <b>-${amount:,.2f}</b>\n• Reason: {reason}\n• Remaining Balance: <b>${new_balance:,.2f}</b>",
                    main_menu_keyboard()
                )

            elif state == "AWAITING_INCOME":
                bank_name = state_data
                parts = text.split(maxsplit=1)
                try:
                    amount = float(parts[0])
                    if amount <= 0:
                        raise ValueError
                except (ValueError, IndexError):
                    await send_tg_message(chat_id, "❌ Enter a valid positive amount and source.", cancel_keyboard())
                    return Response(status_code=200)

                source = parts[1] if len(parts) > 1 else "Unspecified"
                accs = await query_turso("SELECT balance FROM accounts WHERE user_id = ? AND bank_name = ?", [user_id, bank_name])
                if not accs:
                    await send_tg_message(chat_id, "Account not found.", main_menu_keyboard())
                    return Response(status_code=200)

                new_balance = float(accs[0][0]) + amount
                await query_turso("UPDATE accounts SET balance = ? WHERE user_id = ? AND bank_name = ?", [new_balance, user_id, bank_name])
                await query_turso(
                    "INSERT INTO transactions (user_id, bank_name, type, amount, description) VALUES (?, ?, 'income', ?, ?)",
                    [user_id, bank_name, amount, source]
                )
                await query_turso("UPDATE users SET state = NULL, state_data = NULL WHERE user_id = ?", [user_id])
                await send_tg_message(
                    chat_id,
                    f"💰 <b>Income Logged!</b>\n• Account: <b>{bank_name}</b>\n• Amount: <b>+${amount:,.2f}</b>\n• Source: {source}\n• New Balance: <b>${new_balance:,.2f}</b>",
                    main_menu_keyboard()
                )

        # 2. Button Callback Received
        elif "callback_query" in data:
            cb = data["callback_query"]
            cb_id = cb["id"]
            user_id = cb["from"]["id"]
            chat_id = cb["message"]["chat"]["id"]
            message_id = cb["message"]["message_id"]
            cb_data = cb.get("data", "")

            await answer_tg_callback(cb_id)

            if cb_data == "btn_addbank":
                await query_turso("UPDATE users SET state = 'AWAITING_ADD_BANK', state_data = NULL WHERE user_id = ?", [user_id])
                await edit_tg_message(
                    chat_id,
                    message_id,
                    "🏦 <b>Add / Update Bank Account</b>\n\nSend: <code>&lt;BankName&gt; &lt;Balance&gt;</code>\n<i>Example:</i> <code>CBE 5000</code>",
                    cancel_keyboard()
                )

            elif cb_data == "btn_spend":
                kb = await get_bank_selection_keyboard(user_id, "spend_bank")
                if not kb:
                    await edit_tg_message(chat_id, message_id, "⚠️ No accounts found. Tap 'Add / Edit Bank' first.", main_menu_keyboard())
                else:
                    await edit_tg_message(chat_id, message_id, "💸 <b>Select account spent from:</b>", kb)

            elif cb_data == "btn_income":
                kb = await get_bank_selection_keyboard(user_id, "income_bank")
                if not kb:
                    await edit_tg_message(chat_id, message_id, "⚠️ No accounts found. Tap 'Add / Edit Bank' first.", main_menu_keyboard())
                else:
                    await edit_tg_message(chat_id, message_id, "💰 <b>Select account received in:</b>", kb)

            elif cb_data.startswith("spend_bank:"):
                bank_name = cb_data.split(":", 1)[1]
                await query_turso("UPDATE users SET state = 'AWAITING_SPEND', state_data = ? WHERE user_id = ?", [bank_name, user_id])
                await edit_tg_message(
                    chat_id,
                    message_id,
                    f"💸 <b>Selected:</b> <code>{bank_name}</code>\n\nSend the <b>amount</b> and <b>reason</b>.\n<i>Example:</i> <code>150 Lunch with friends</code>",
                    cancel_keyboard()
                )

            elif cb_data.startswith("income_bank:"):
                bank_name = cb_data.split(":", 1)[1]
                await query_turso("UPDATE users SET state = 'AWAITING_INCOME', state_data = ? WHERE user_id = ?", [bank_name, user_id])
                await edit_tg_message(
                    chat_id,
                    message_id,
                    f"💰 <b>Selected:</b> <code>{bank_name}</code>\n\nSend the <b>amount</b> and <b>source</b>.\n<i>Example:</i> <code>500 Freelance gig</code>",
                    cancel_keyboard()
                )

            elif cb_data == "btn_balance":
                report = await get_balances_text(user_id)
                await edit_tg_message(chat_id, message_id, report, main_menu_keyboard())

            elif cb_data == "btn_summary":
                report = await build_daily_summary(user_id)
                await edit_tg_message(chat_id, message_id, report, main_menu_keyboard())

            elif cb_data == "btn_cancel":
                await query_turso("UPDATE users SET state = NULL, state_data = NULL WHERE user_id = ?", [user_id])
                await edit_tg_message(chat_id, message_id, "Action cancelled. Choose an option:", main_menu_keyboard())

    except Exception as e:
        logging.error(f"Webhook Error: {e}", exc_info=True)

    return Response(status_code=200)


@app.get("/api/daily_summary")
@app.get("/daily_summary")
async def daily_summary():
    await init_db()
    users = await query_turso("SELECT user_id, chat_id FROM users")
    for user_id, chat_id in users:
        try:
            report = await build_daily_summary(int(user_id))
            await send_tg_message(int(chat_id), report, main_menu_keyboard())
        except Exception as e:
            logging.error(f"Error sending summary to {user_id}: {e}")
    return {"status": "success", "count": len(users)}


@app.get("/")
@app.get("/api/webhook")
async def root():
    return {"status": "ok", "message": "Money Tracker Bot is running"}
