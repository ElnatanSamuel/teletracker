import datetime
import logging
import os
from contextlib import asynccontextmanager
from dotenv import load_dotenv
from fastapi import FastAPI, Request, Response
import libsql_client
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ApplicationBuilder

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)

BOT_TOKEN = os.getenv("BOT_TOKEN")
RAW_DB_URL = os.getenv("TURSO_DATABASE_URL", "").strip()
TURSO_AUTH_TOKEN = os.getenv("TURSO_AUTH_TOKEN", "").strip()

if RAW_DB_URL.startswith("libsql://"):
    TURSO_DB_URL = RAW_DB_URL.replace("libsql://", "https://")
elif not RAW_DB_URL.startswith("http://") and not RAW_DB_URL.startswith("https://"):
    TURSO_DB_URL = f"https://{RAW_DB_URL}"
else:
    TURSO_DB_URL = RAW_DB_URL


def get_db_client():
    if not TURSO_DB_URL or not TURSO_AUTH_TOKEN:
        raise ValueError("TURSO_DATABASE_URL and TURSO_AUTH_TOKEN must be set.")
    return libsql_client.create_client(url=TURSO_DB_URL, auth_token=TURSO_AUTH_TOKEN)


async def init_db():
    async with get_db_client() as client:
        await client.execute(
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
        await client.execute(
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
        await client.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                chat_id INTEGER,
                state TEXT,
                state_data TEXT
            )
        """
        )


# --- Keyboards ---

def main_menu_keyboard():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("💸 Spend", callback_data="btn_spend"),
                InlineKeyboardButton("💰 Income", callback_data="btn_income"),
            ],
            [
                InlineKeyboardButton("🏦 Add / Edit Bank", callback_data="btn_addbank"),
                InlineKeyboardButton("📊 Balances", callback_data="btn_balance"),
            ],
            [
                InlineKeyboardButton("📈 Today's Summary", callback_data="btn_summary"),
            ],
        ]
    )


def cancel_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="btn_cancel")]])


async def get_bank_selection_keyboard(user_id: int, action_prefix: str):
    async with get_db_client() as client:
        rs = await client.execute("SELECT bank_name, balance FROM accounts WHERE user_id = ?", [user_id])
        banks = rs.rows

    if not banks:
        return None

    buttons = []
    for row in banks:
        bank_name = row[0]
        balance = float(row[1])
        buttons.append(
            [InlineKeyboardButton(f"{bank_name} (${balance:,.2f})", callback_data=f"{action_prefix}:{bank_name}")]
        )
    buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="btn_cancel")])
    return InlineKeyboardMarkup(buttons)


# --- Reports ---

async def get_balances_text(user_id: int) -> str:
    async with get_db_client() as client:
        rs = await client.execute("SELECT bank_name, balance FROM accounts WHERE user_id = ?", [user_id])
        rows = rs.rows

    if not rows:
        return "🏦 **No accounts found.** Tap 'Add / Edit Bank' to create one."

    total = sum(float(r[1]) for r in rows)
    lines = [f"• **{r[0]}**: ${float(r[1]):,.2f}" for r in rows]
    return "🏦 **Current Balances:**\n\n" + "\n".join(lines) + f"\n\n**Total Net Worth:** `${total:,.2f}`"


async def build_daily_summary(user_id: int) -> str:
    today_str = datetime.date.today().isoformat()
    async with get_db_client() as client:
        tx_rs = await client.execute(
            "SELECT bank_name, type, amount, description FROM transactions WHERE user_id = ? AND date(timestamp) = date('now') ORDER BY timestamp ASC",
            [user_id],
        )
        acc_rs = await client.execute("SELECT bank_name, balance FROM accounts WHERE user_id = ?", [user_id])

    txs = tx_rs.rows
    accounts = acc_rs.rows

    spent = sum(float(t[2]) for t in txs if t[1] == "expense")
    earned = sum(float(t[2]) for t in txs if t[1] == "income")
    net = earned - spent
    total_balance = sum(float(a[1]) for a in accounts)

    msg = f"📊 **Daily Summary ({today_str})**\n\n• **Spent Today:** -${spent:,.2f}\n• **Income Today:** +${earned:,.2f}\n• **Net Change:** {'+$' if net >= 0 else '-$'}{abs(net):,.2f}\n\n"

    if txs:
        msg += "**Today's Transactions:**\n"
        for bank, tx_type, amt, desc in txs:
            sign = "-" if tx_type == "expense" else "+"
            msg += f"  • `{sign}${float(amt):,.2f}` ({bank}) - {desc}\n"
        msg += "\n"
    else:
        msg += "_No transactions recorded today._\n\n"

    msg += "**Balances:**\n"
    for bank, bal in accounts:
        msg += f"  • {bank}: ${float(bal):,.2f}\n"
    msg += f"**Total Net Worth:** `${total_balance:,.2f}`"
    return msg


# --- Bot Application Setup ---

bot_app = ApplicationBuilder().token(BOT_TOKEN).build()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    await bot_app.initialize()
    yield
    await bot_app.shutdown()


app = FastAPI(lifespan=lifespan)


# --- Core Logic Handlers ---

async def process_telegram_update(update: Update):
    if update.message:
        user_id = update.message.from_user.id
        chat_id = update.message.chat_id
        text = update.message.text.strip() if update.message.text else ""

        if text.startswith("/start"):
            async with get_db_client() as client:
                await client.execute(
                    "INSERT INTO users (user_id, chat_id, state, state_data) VALUES (?, ?, NULL, NULL) "
                    "ON CONFLICT(user_id) DO UPDATE SET chat_id = excluded.chat_id, state = NULL, state_data = NULL",
                    [user_id, chat_id],
                )
            await bot_app.bot.send_message(
                chat_id=chat_id,
                text="👋 **Welcome to Money Tracker!**\n\nTap an option below:",
                reply_markup=main_menu_keyboard(),
                parse_mode="Markdown",
            )
            return

        # Check current user state
        async with get_db_client() as client:
            rs = await client.execute("SELECT state, state_data FROM users WHERE user_id = ?", [user_id])
            user_row = rs.rows[0] if rs.rows else None

        if not user_row or not user_row[0]:
            await bot_app.bot.send_message(
                chat_id=chat_id,
                text="Please select an action from the menu:",
                reply_markup=main_menu_keyboard(),
            )
            return

        state = user_row[0]
        state_data = user_row[1]

        if state == "AWAITING_ADD_BANK":
            parts = text.split()
            if len(parts) < 2:
                await bot_app.bot.send_message(
                    chat_id=chat_id,
                    text="❌ Send: `<BankName> <Balance>` (e.g., `CBE 5000`)",
                    reply_markup=cancel_keyboard(),
                    parse_mode="Markdown",
                )
                return

            bank_name = parts[0].capitalize()
            try:
                balance = float(parts[1])
            except ValueError:
                await bot_app.bot.send_message(chat_id=chat_id, text="❌ Please enter a valid number.", reply_markup=cancel_keyboard())
                return

            async with get_db_client() as client:
                await client.execute(
                    "INSERT INTO accounts (user_id, bank_name, balance) VALUES (?, ?, ?) "
                    "ON CONFLICT(user_id, bank_name) DO UPDATE SET balance = excluded.balance",
                    [user_id, bank_name, balance],
                )
                await client.execute("UPDATE users SET state = NULL, state_data = NULL WHERE user_id = ?", [user_id])

            await bot_app.bot.send_message(
                chat_id=chat_id,
                text=f"✅ Account **{bank_name}** set to **${balance:,.2f}**.",
                reply_markup=main_menu_keyboard(),
                parse_mode="Markdown",
            )

        elif state == "AWAITING_SPEND":
            bank_name = state_data
            parts = text.split(maxsplit=1)
            try:
                amount = float(parts[0])
                if amount <= 0:
                    raise ValueError
            except (ValueError, IndexError):
                await bot_app.bot.send_message(chat_id=chat_id, text="❌ Enter a valid positive amount and reason.", reply_markup=cancel_keyboard())
                return

            reason = parts[1] if len(parts) > 1 else "Unspecified"

            async with get_db_client() as client:
                rs = await client.execute("SELECT balance FROM accounts WHERE user_id = ? AND bank_name = ?", [user_id, bank_name])
                if not rs.rows:
                    await bot_app.bot.send_message(chat_id=chat_id, text="Account not found.", reply_markup=main_menu_keyboard())
                    return

                new_balance = float(rs.rows[0][0]) - amount
                await client.execute("UPDATE accounts SET balance = ? WHERE user_id = ? AND bank_name = ?", [new_balance, user_id, bank_name])
                await client.execute(
                    "INSERT INTO transactions (user_id, bank_name, type, amount, description) VALUES (?, ?, 'expense', ?, ?)",
                    [user_id, bank_name, amount, reason],
                )
                await client.execute("UPDATE users SET state = NULL, state_data = NULL WHERE user_id = ?", [user_id])

            await bot_app.bot.send_message(
                chat_id=chat_id,
                text=f"💸 **Expense Logged!**\n• {bank_name}: -${amount:,.2f}\n• Reason: {reason}\n• Remaining: `${new_balance:,.2f}`",
                reply_markup=main_menu_keyboard(),
                parse_mode="Markdown",
            )

        elif state == "AWAITING_INCOME":
            bank_name = state_data
            parts = text.split(maxsplit=1)
            try:
                amount = float(parts[0])
                if amount <= 0:
                    raise ValueError
            except (ValueError, IndexError):
                await bot_app.bot.send_message(chat_id=chat_id, text="❌ Enter a valid positive amount and source.", reply_markup=cancel_keyboard())
                return

            source = parts[1] if len(parts) > 1 else "Unspecified"

            async with get_db_client() as client:
                rs = await client.execute("SELECT balance FROM accounts WHERE user_id = ? AND bank_name = ?", [user_id, bank_name])
                if not rs.rows:
                    await bot_app.bot.send_message(chat_id=chat_id, text="Account not found.", reply_markup=main_menu_keyboard())
                    return

                new_balance = float(rs.rows[0][0]) + amount
                await client.execute("UPDATE accounts SET balance = ? WHERE user_id = ? AND bank_name = ?", [new_balance, user_id, bank_name])
                await client.execute(
                    "INSERT INTO transactions (user_id, bank_name, type, amount, description) VALUES (?, ?, 'income', ?, ?)",
                    [user_id, bank_name, amount, source],
                )
                await client.execute("UPDATE users SET state = NULL, state_data = NULL WHERE user_id = ?", [user_id])

            await bot_app.bot.send_message(
                chat_id=chat_id,
                text=f"💰 **Income Logged!**\n• {bank_name}: +${amount:,.2f}\n• Source: {source}\n• New Balance: `${new_balance:,.2f}`",
                reply_markup=main_menu_keyboard(),
                parse_mode="Markdown",
            )

    elif update.callback_query:
        query = update.callback_query
        await query.answer()
        data = query.data
        user_id = query.from_user.id
        chat_id = query.message.chat_id
        message_id = query.message.message_id

        if data == "btn_addbank":
            async with get_db_client() as client:
                await client.execute("UPDATE users SET state = 'AWAITING_ADD_BANK', state_data = NULL WHERE user_id = ?", [user_id])
            await bot_app.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text="🏦 **Add / Update Bank Account**\n\nSend: `<BankName> <Balance>`\n_Example:_ `CBE 5000`",
                reply_markup=cancel_keyboard(),
                parse_mode="Markdown",
            )

        elif data == "btn_spend":
            kb = await get_bank_selection_keyboard(user_id, "spend_bank")
            if not kb:
                await bot_app.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text="⚠️ No accounts found. Tap **Add / Edit Bank** first.",
                    reply_markup=main_menu_keyboard(),
                    parse_mode="Markdown",
                )
                return
            await bot_app.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text="💸 **Select account spent from:**",
                reply_markup=kb,
                parse_mode="Markdown",
            )

        elif data == "btn_income":
            kb = await get_bank_selection_keyboard(user_id, "income_bank")
            if not kb:
                await bot_app.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text="⚠️ No accounts found. Tap **Add / Edit Bank** first.",
                    reply_markup=main_menu_keyboard(),
                    parse_mode="Markdown",
                )
                return
            await bot_app.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text="💰 **Select account received in:**",
                reply_markup=kb,
                parse_mode="Markdown",
            )

        elif data.startswith("spend_bank:"):
            bank_name = data.split(":", 1)[1]
            async with get_db_client() as client:
                await client.execute("UPDATE users SET state = 'AWAITING_SPEND', state_data = ? WHERE user_id = ?", [bank_name, user_id])
            await bot_app.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=f"💸 **Selected:** {bank_name}\n\nSend: `<Amount> <Reason>`\n_Example:_ `150 Lunch`",
                reply_markup=cancel_keyboard(),
                parse_mode="Markdown",
            )

        elif data.startswith("income_bank:"):
            bank_name = data.split(":", 1)[1]
            async with get_db_client() as client:
                await client.execute("UPDATE users SET state = 'AWAITING_INCOME', state_data = ? WHERE user_id = ?", [bank_name, user_id])
            await bot_app.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=f"💰 **Selected:** {bank_name}\n\nSend: `<Amount> <Source>`\n_Example:_ `500 Salary`",
                reply_markup=cancel_keyboard(),
                parse_mode="Markdown",
            )

        elif data == "btn_balance":
            report = await get_balances_text(user_id)
            await bot_app.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=report,
                reply_markup=main_menu_keyboard(),
                parse_mode="Markdown",
            )

        elif data == "btn_summary":
            report = await build_daily_summary(user_id)
            await bot_app.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=report,
                reply_markup=main_menu_keyboard(),
                parse_mode="Markdown",
            )

        elif data == "btn_cancel":
            async with get_db_client() as client:
                await client.execute("UPDATE users SET state = NULL, state_data = NULL WHERE user_id = ?", [user_id])
            await bot_app.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text="Action cancelled.",
                reply_markup=main_menu_keyboard(),
            )


# --- FastAPI Endpoints ---

@app.post("/api/webhook")
@app.post("/")
async def webhook(request: Request):
    try:
        data = await request.json()
        update = Update.de_json(data, bot_app.bot)
        await process_telegram_update(update)
    except Exception as e:
        logging.error(f"Error handling update: {e}")
    return Response(status_code=200)


@app.get("/api/set_webhook")
async def set_webhook(request: Request):
    base_url = str(request.base_url).rstrip("/")
    # Force HTTPS for Vercel
    if base_url.startswith("http://"):
        base_url = base_url.replace("http://", "https://")
    webhook_url = f"{base_url}/api/webhook"
    success = await bot_app.bot.set_webhook(webhook_url)
    return {"status": "ok", "webhook_url": webhook_url, "telegram_response": success}


@app.get("/api/daily_summary")
async def daily_summary():
    async with get_db_client() as client:
        rs = await client.execute("SELECT user_id, chat_id FROM users")
        users = rs.rows

    for user_id, chat_id in users:
        try:
            report = await build_daily_summary(int(user_id))
            await bot_app.bot.send_message(
                chat_id=int(chat_id),
                text=report,
                reply_markup=main_menu_keyboard(),
                parse_mode="Markdown",
            )
        except Exception as e:
            logging.error(f"Error sending daily summary to {user_id}: {e}")
    return {"status": "success", "users_notified": len(users)}
