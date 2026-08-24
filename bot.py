import asyncio
import datetime
import logging
import os
from aiohttp import web
from dotenv import load_dotenv
import libsql_experimental as libsql
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

load_dotenv()

# Logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)

BOT_TOKEN = os.getenv("BOT_TOKEN")
TURSO_DB_URL = os.getenv("TURSO_DATABASE_URL")
TURSO_AUTH_TOKEN = os.getenv("TURSO_AUTH_TOKEN")
PORT = int(os.getenv("PORT", 8080))

# Conversation States
(
    MAIN_MENU,
    AWAITING_BANK_INFO,
    SELECTING_SPEND_BANK,
    AWAITING_SPEND_DETAILS,
    SELECTING_INCOME_BANK,
    AWAITING_INCOME_DETAILS,
) = range(6)


# --- Database Connection & Setup ---

def get_db():
    if not TURSO_DB_URL or not TURSO_AUTH_TOKEN:
        raise ValueError("TURSO_DATABASE_URL and TURSO_AUTH_TOKEN must be configured.")
    return libsql.connect(TURSO_DB_URL, auth_token=TURSO_AUTH_TOKEN)


def init_db():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
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
    cursor.execute(
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
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            chat_id INTEGER
        )
    """
    )
    conn.commit()
    conn.close()


# --- Keyboards ---

def main_menu_keyboard():
    keyboard = [
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
    return InlineKeyboardMarkup(keyboard)


def cancel_keyboard():
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("❌ Cancel", callback_data="btn_cancel")]]
    )


def bank_selection_keyboard(user_id: int, action_prefix: str):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT bank_name, balance FROM accounts WHERE user_id = ?", (user_id,))
    banks = cursor.fetchall()
    conn.close()

    if not banks:
        return None

    buttons = []
    for bank_name, balance in banks:
        buttons.append(
            [InlineKeyboardButton(f"{bank_name} (${balance:,.2f})", callback_data=f"{action_prefix}:{bank_name}")]
        )
    buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="btn_cancel")])
    return InlineKeyboardMarkup(buttons)


# --- Handlers ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT OR REPLACE INTO users (user_id, chat_id) VALUES (?, ?)",
        (user_id, chat_id),
    )
    conn.commit()
    conn.close()

    text = (
        "👋 **Welcome to Money Tracker!**\n\n"
        "Tap an option below to manage your money:"
    )
    await update.message.reply_text(text, reply_markup=main_menu_keyboard(), parse_mode="Markdown")
    return MAIN_MENU


async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = update.effective_user.id

    if data == "btn_addbank":
        await query.edit_message_text(
            "🏦 **Add or Update Bank Account**\n\n"
            "Send the **Bank Name** and **Current Balance**.\n\n"
            "_Example:_ `CBE 5000` or `Telebirr 1200.50`",
            reply_markup=cancel_keyboard(),
            parse_mode="Markdown",
        )
        return AWAITING_BANK_INFO

    elif data == "btn_spend":
        kb = bank_selection_keyboard(user_id, "spend_bank")
        if not kb:
            await query.edit_message_text(
                "⚠️ No bank accounts found. Please add a bank account first.",
                reply_markup=main_menu_keyboard(),
                parse_mode="Markdown",
            )
            return MAIN_MENU

        await query.edit_message_text(
            "💸 **Select the account you spent from:**",
            reply_markup=kb,
            parse_mode="Markdown",
        )
        return SELECTING_SPEND_BANK

    elif data == "btn_income":
        kb = bank_selection_keyboard(user_id, "income_bank")
        if not kb:
            await query.edit_message_text(
                "⚠️ No bank accounts found. Please add a bank account first.",
                reply_markup=main_menu_keyboard(),
                parse_mode="Markdown",
            )
            return MAIN_MENU

        await query.edit_message_text(
            "💰 **Select the account you received money in:**",
            reply_markup=kb,
            parse_mode="Markdown",
        )
        return SELECTING_INCOME_BANK

    elif data == "btn_balance":
        report = get_balances_text(user_id)
        await query.edit_message_text(report, reply_markup=main_menu_keyboard(), parse_mode="Markdown")
        return MAIN_MENU

    elif data == "btn_summary":
        report = build_daily_summary(user_id)
        await query.edit_message_text(report, reply_markup=main_menu_keyboard(), parse_mode="Markdown")
        return MAIN_MENU

    elif data == "btn_cancel":
        await query.edit_message_text("Action cancelled. What would you like to do?", reply_markup=main_menu_keyboard())
        return MAIN_MENU

    return MAIN_MENU


# --- Add Bank Flow ---

async def handle_bank_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text.strip().split()

    if len(text) < 2:
        await update.message.reply_text(
            "❌ Invalid format. Send: `<BankName> <Balance>` (e.g. `CBE 5000`)",
            reply_markup=cancel_keyboard(),
            parse_mode="Markdown",
        )
        return AWAITING_BANK_INFO

    bank_name = text[0].capitalize()
    try:
        balance = float(text[1])
    except ValueError:
        await update.message.reply_text("❌ Please enter a valid number for balance.", reply_markup=cancel_keyboard())
        return AWAITING_BANK_INFO

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO accounts (user_id, bank_name, balance)
        VALUES (?, ?, ?)
        ON CONFLICT(user_id, bank_name) DO UPDATE SET balance = excluded.balance
    """,
        (user_id, bank_name, balance),
    )
    conn.commit()
    conn.close()

    await update.message.reply_text(
        f"✅ Account **{bank_name}** set to **${balance:,.2f}**.",
        reply_markup=main_menu_keyboard(),
        parse_mode="Markdown",
    )
    return MAIN_MENU


# --- Spend Flow ---

async def handle_spend_bank_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "btn_cancel":
        await query.edit_message_text("Cancelled.", reply_markup=main_menu_keyboard())
        return MAIN_MENU

    bank_name = query.data.split(":")[1]
    context.user_data["selected_bank"] = bank_name

    await query.edit_message_text(
        f"💸 **Selected:** {bank_name}\n\n"
        "Send the **amount** and **reason**.\n\n"
        "_Example:_ `150 Lunch with friends`",
        reply_markup=cancel_keyboard(),
        parse_mode="Markdown",
    )
    return AWAITING_SPEND_DETAILS


async def handle_spend_details(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    bank_name = context.user_data.get("selected_bank")
    text = update.message.text.strip().split(maxsplit=1)

    if not text:
        await update.message.reply_text("Please enter an amount and reason.", reply_markup=cancel_keyboard())
        return AWAITING_SPEND_DETAILS

    try:
        amount = float(text[0])
        if amount <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Please provide a valid positive number for amount.", reply_markup=cancel_keyboard())
        return AWAITING_SPEND_DETAILS

    reason = text[1] if len(text) > 1 else "Unspecified"

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT balance FROM accounts WHERE user_id = ? AND bank_name = ?", (user_id, bank_name))
    row = cursor.fetchone()

    if not row:
        conn.close()
        await update.message.reply_text("Account not found.", reply_markup=main_menu_keyboard())
        return MAIN_MENU

    new_balance = row[0] - amount
    cursor.execute("UPDATE accounts SET balance = ? WHERE user_id = ? AND bank_name = ?", (new_balance, user_id, bank_name))
    cursor.execute(
        "INSERT INTO transactions (user_id, bank_name, type, amount, description) VALUES (?, ?, 'expense', ?, ?)",
        (user_id, bank_name, amount, reason),
    )
    conn.commit()
    conn.close()

    await update.message.reply_text(
        f"💸 **Expense Logged!**\n\n"
        f"• **Bank:** {bank_name}\n"
        f"• **Amount:** -${amount:,.2f}\n"
        f"• **Reason:** {reason}\n"
        f"• **Remaining Balance:** `${new_balance:,.2f}`",
        reply_markup=main_menu_keyboard(),
        parse_mode="Markdown",
    )
    return MAIN_MENU


# --- Income Flow ---

async def handle_income_bank_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "btn_cancel":
        await query.edit_message_text("Cancelled.", reply_markup=main_menu_keyboard())
        return MAIN_MENU

    bank_name = query.data.split(":")[1]
    context.user_data["selected_bank"] = bank_name

    await query.edit_message_text(
        f"💰 **Selected:** {bank_name}\n\n"
        "Send the **amount** and **source**.\n\n"
        "_Example:_ `500 Freelance project`",
        reply_markup=cancel_keyboard(),
        parse_mode="Markdown",
    )
    return AWAITING_INCOME_DETAILS


async def handle_income_details(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    bank_name = context.user_data.get("selected_bank")
    text = update.message.text.strip().split(maxsplit=1)

    if not text:
        await update.message.reply_text("Please enter an amount and source.", reply_markup=cancel_keyboard())
        return AWAITING_INCOME_DETAILS

    try:
        amount = float(text[0])
        if amount <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Please enter a valid positive number for amount.", reply_markup=cancel_keyboard())
        return AWAITING_INCOME_DETAILS

    source = text[1] if len(text) > 1 else "Unspecified"

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT balance FROM accounts WHERE user_id = ? AND bank_name = ?", (user_id, bank_name))
    row = cursor.fetchone()

    if not row:
        conn.close()
        await update.message.reply_text("Account not found.", reply_markup=main_menu_keyboard())
        return MAIN_MENU

    new_balance = row[0] + amount
    cursor.execute("UPDATE accounts SET balance = ? WHERE user_id = ? AND bank_name = ?", (new_balance, user_id, bank_name))
    cursor.execute(
        "INSERT INTO transactions (user_id, bank_name, type, amount, description) VALUES (?, ?, 'income', ?, ?)",
        (user_id, bank_name, amount, source),
    )
    conn.commit()
    conn.close()

    await update.message.reply_text(
        f"💰 **Income Logged!**\n\n"
        f"• **Bank:** {bank_name}\n"
        f"• **Amount:** +${amount:,.2f}\n"
        f"• **Source:** {source}\n"
        f"• **New Balance:** `${new_balance:,.2f}`",
        reply_markup=main_menu_keyboard(),
        parse_mode="Markdown",
    )
    return MAIN_MENU


# --- Reports ---

def get_balances_text(user_id: int) -> str:
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT bank_name, balance FROM accounts WHERE user_id = ?", (user_id,))
    rows = cursor.fetchall()
    conn.close()

    if not rows:
        return "🏦 **No accounts found.** Tap 'Add / Edit Bank' to add one."

    total = sum(r[1] for r in rows)
    lines = [f"• **{name}**: ${bal:,.2f}" for name, bal in rows]
    return "🏦 **Current Balances:**\n\n" + "\n".join(lines) + f"\n\n**Total Net Worth:** `${total:,.2f}`"


def build_daily_summary(user_id: int) -> str:
    conn = get_db()
    cursor = conn.cursor()
    today_str = datetime.date.today().isoformat()

    cursor.execute(
        """
        SELECT bank_name, type, amount, description
        FROM transactions
        WHERE user_id = ? AND date(timestamp) = date('now')
        ORDER BY timestamp ASC
    """,
        (user_id,),
    )
    txs = cursor.fetchall()

    cursor.execute("SELECT bank_name, balance FROM accounts WHERE user_id = ?", (user_id,))
    accounts = cursor.fetchall()
    conn.close()

    spent = sum(t[2] for t in txs if t[1] == "expense")
    earned = sum(t[2] for t in txs if t[1] == "income")
    net = earned - spent
    total_balance = sum(a[1] for a in accounts)

    msg = f"📊 **Daily Summary ({today_str})**\n\n"
    msg += f"• **Total Spent Today:** -${spent:,.2f}\n"
    msg += f"• **Total Income Today:** +${earned:,.2f}\n"
    msg += f"• **Net Change:** {'+$' if net >= 0 else '-$'}{abs(net):,.2f}\n\n"

    if txs:
        msg += "**Today's Transactions:**\n"
        for bank, tx_type, amt, desc in txs:
            sign = "-" if tx_type == "expense" else "+"
            msg += f"  • `{sign}${amt:,.2f}` ({bank}) - {desc}\n"
        msg += "\n"
    else:
        msg += "_No transactions recorded today._\n\n"

    msg += "**Balances:**\n"
    for bank, bal in accounts:
        msg += f"  • {bank}: ${bal:,.2f}\n"
    msg += f"**Total Net Worth:** `${total_balance:,.2f}`"

    return msg


async def scheduled_daily_summary(context: ContextTypes.DEFAULT_TYPE):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT user_id, chat_id FROM users")
    users = cursor.fetchall()
    conn.close()

    for user_id, chat_id in users:
        try:
            report = build_daily_summary(user_id)
            await context.bot.send_message(
                chat_id=chat_id,
                text=report,
                reply_markup=main_menu_keyboard(),
                parse_mode="Markdown",
            )
        except Exception as e:
            logging.error(f"Error sending summary to {user_id}: {e}")


# --- Web Healthcheck for Render ---

async def health_check(request):
    return web.Response(text="Bot is running")


async def run_web_server(app):
    web_app = web.Application()
    web_app.router.add_get("/", health_check)
    web_app.router.add_get("/health", health_check)
    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logging.info(f"Healthcheck server listening on port {PORT}")


async def post_init(application):
    await run_web_server(application)


def main():
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN is not set.")
    init_db()

    app = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).build()

    # Setup Conversation Handler for button UI
    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            CallbackQueryHandler(menu_callback),
        ],
        states={
            MAIN_MENU: [
                CallbackQueryHandler(menu_callback),
            ],
            AWAITING_BANK_INFO: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_bank_input),
                CallbackQueryHandler(menu_callback, pattern="^btn_cancel$"),
            ],
            SELECTING_SPEND_BANK: [
                CallbackQueryHandler(handle_spend_bank_select, pattern="^spend_bank:"),
                CallbackQueryHandler(menu_callback, pattern="^btn_cancel$"),
            ],
            AWAITING_SPEND_DETAILS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_spend_details),
                CallbackQueryHandler(menu_callback, pattern="^btn_cancel$"),
            ],
            SELECTING_INCOME_BANK: [
                CallbackQueryHandler(handle_income_bank_select, pattern="^income_bank:"),
                CallbackQueryHandler(menu_callback, pattern="^btn_cancel$"),
            ],
            AWAITING_INCOME_DETAILS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_income_details),
                CallbackQueryHandler(menu_callback, pattern="^btn_cancel$"),
            ],
        },
        fallbacks=[CommandHandler("start", start)],
        allow_reentry=True,
    )

    app.add_handler(conv_handler)

    # Schedule daily report at 21:00 UTC
    if app.job_queue:
        app.job_queue.run_daily(
            scheduled_daily_summary,
            time=datetime.time(hour=21, minute=0, second=0),
            name="daily_summary_job",
        )

    logging.info("Button-Driven Money Tracker Bot started.")
    app.run_polling()


if __name__ == "__main__":
    main()