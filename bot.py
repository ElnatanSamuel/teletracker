import asyncio
import datetime
import logging
import os
from aiohttp import web
from dotenv import load_dotenv
import libsql_experimental as libsql
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
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


def get_db():
    """Connect to Turso Cloud SQLite database."""
    if not TURSO_DB_URL or not TURSO_AUTH_TOKEN:
        raise ValueError("TURSO_DATABASE_URL and TURSO_AUTH_TOKEN must be configured.")
    return libsql.connect(TURSO_DB_URL, auth_token=TURSO_AUTH_TOKEN)


def init_db():
    """Create database tables if they do not exist."""
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


# --- Command Handlers ---

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
        "👋 **Welcome to Money Tracker Bot!**\n\n"
        "**Commands:**\n"
        "• `/addbank <name> <balance>` - Add or update a bank balance\n"
        "• `/spend <bank> <amount> <reason>` - Record an expense\n"
        "• `/income <bank> <amount> <source>` - Record income\n"
        "• `/balance` - Show balances across all accounts\n"
        "• `/summary` - View today's financial summary\n\n"
        "_Daily summaries are sent automatically every day at 21:00._"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def add_bank(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    args = context.args

    if len(args) < 2:
        await update.message.reply_text("Usage: `/addbank <bank_name> <balance>`\nExample: `/addbank Chase 1500`", parse_mode="Markdown")
        return

    bank_name = args[0].strip().capitalize()
    try:
        balance = float(args[1])
    except ValueError:
        await update.message.reply_text("Please enter a valid numeric balance.")
        return

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

    await update.message.reply_text(f"✅ Account **{bank_name}** set to **${balance:,.2f}**.", parse_mode="Markdown")


async def spend(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    args = context.args

    if len(args) < 3:
        await update.message.reply_text("Usage: `/spend <bank> <amount> <reason>`\nExample: `/spend Chase 14.50 Coffee and sandwich`", parse_mode="Markdown")
        return

    bank_name = args[0].strip().capitalize()
    try:
        amount = float(args[1])
        if amount <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Please enter a valid positive amount.")
        return

    reason = " ".join(args[2:])
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("SELECT balance FROM accounts WHERE user_id = ? AND bank_name = ?", (user_id, bank_name))
    row = cursor.fetchone()

    if not row:
        conn.close()
        await update.message.reply_text(f"Bank '{bank_name}' not found. Use `/addbank {bank_name} <balance>` first.", parse_mode="Markdown")
        return

    new_balance = row[0] - amount
    cursor.execute("UPDATE accounts SET balance = ? WHERE user_id = ? AND bank_name = ?", (new_balance, user_id, bank_name))
    cursor.execute(
        "INSERT INTO transactions (user_id, bank_name, type, amount, description) VALUES (?, ?, 'expense', ?, ?)",
        (user_id, bank_name, amount, reason),
    )
    conn.commit()
    conn.close()

    await update.message.reply_text(
        f"💸 **Expense Logged**\n"
        f"• Bank: {bank_name}\n"
        f"• Amount: -${amount:,.2f}\n"
        f"• Note: {reason}\n"
        f"• Remaining: **${new_balance:,.2f}**",
        parse_mode="Markdown",
    )


async def income(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    args = context.args

    if len(args) < 3:
        await update.message.reply_text("Usage: `/income <bank> <amount> <source>`\nExample: `/income Chase 250.00 Freelance gig`", parse_mode="Markdown")
        return

    bank_name = args[0].strip().capitalize()
    try:
        amount = float(args[1])
        if amount <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Please enter a valid positive amount.")
        return

    source = " ".join(args[2:])
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("SELECT balance FROM accounts WHERE user_id = ? AND bank_name = ?", (user_id, bank_name))
    row = cursor.fetchone()

    if not row:
        conn.close()
        await update.message.reply_text(f"Bank '{bank_name}' not found. Use `/addbank {bank_name} <balance>` first.", parse_mode="Markdown")
        return

    new_balance = row[0] + amount
    cursor.execute("UPDATE accounts SET balance = ? WHERE user_id = ? AND bank_name = ?", (new_balance, user_id, bank_name))
    cursor.execute(
        "INSERT INTO transactions (user_id, bank_name, type, amount, description) VALUES (?, ?, 'income', ?, ?)",
        (user_id, bank_name, amount, source),
    )
    conn.commit()
    conn.close()

    await update.message.reply_text(
        f"💰 **Income Logged**\n"
        f"• Bank: {bank_name}\n"
        f"• Amount: +${amount:,.2f}\n"
        f"• Source: {source}\n"
        f"• New Balance: **${new_balance:,.2f}**",
        parse_mode="Markdown",
    )


async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT bank_name, balance FROM accounts WHERE user_id = ?", (user_id,))
    rows = cursor.fetchall()
    conn.close()

    if not rows:
        await update.message.reply_text("No accounts added yet. Use `/addbank <name> <balance>` to begin.", parse_mode="Markdown")
        return

    total = sum(r[1] for r in rows)
    lines = [f"• **{name}**: ${bal:,.2f}" for name, bal in rows]
    msg = "🏦 **Current Balances:**\n\n" + "\n".join(lines) + f"\n\n**Total Net Worth:** `${total:,.2f}`"
    await update.message.reply_text(msg, parse_mode="Markdown")


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


async def summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    report = build_daily_summary(user_id)
    await update.message.reply_text(report, parse_mode="Markdown")


async def scheduled_daily_summary(context: ContextTypes.DEFAULT_TYPE):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT user_id, chat_id FROM users")
    users = cursor.fetchall()
    conn.close()

    for user_id, chat_id in users:
        try:
            report = build_daily_summary(user_id)
            await context.bot.send_message(chat_id=chat_id, text=report, parse_mode="Markdown")
        except Exception as e:
            logging.error(f"Error sending summary to {user_id}: {e}")


# --- Web Healthcheck for Render Free Web Service ---

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

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("addbank", add_bank))
    app.add_handler(CommandHandler("spend", spend))
    app.add_handler(CommandHandler("income", income))
    app.add_handler(CommandHandler("balance", balance))
    app.add_handler(CommandHandler("summary", summary))

    # Daily report schedule at 21:00 UTC
    if app.job_queue:
        app.job_queue.run_daily(
            scheduled_daily_summary,
            time=datetime.time(hour=21, minute=0, second=0),
            name="daily_summary_job",
        )

    logging.info("Bot started successfully.")
    app.run_polling()


if __name__ == "__main__":
    main()
