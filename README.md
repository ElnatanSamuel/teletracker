# Telegram Money Tracker Bot

A simple Telegram bot to track bank balances, expenses, and income across multiple accounts using **python-telegram-bot**, **Turso (Cloud SQLite)**, and **Render**.

## Features

- **Multi-bank Account Management**: Track balances across different accounts (e.g., Chase, Cash).
- **Expense & Income Logging**: Instantly record logs with descriptions.
- **Instant Balance Check**: Query current bank balances and calculate your total net worth.
- **Daily Summaries**: Provides a daily report summarizing transactions and account status.
- **Lightweight Healthcheck Server**: Designed to run smoothly on Render's free tier.

## Commands

- `/start` - Welcome message and command list.
- `/addbank <name> <balance>` - Add or update a bank balance.
- `/spend <bank> <amount> <reason>` - Record an expense.
- `/income <bank> <amount> <source>` - Record income.
- `/balance` - Show balances across all accounts.
- `/summary` - View today's financial summary.

---

## Setup & Deployment Instructions

### Step 1: Create a Turso Database
1. Go to [turso.tech](https://turso.tech) and sign up for a free account.
2. Create a new database (e.g., `money-tracker`).
3. Copy the database URL (e.g., `libsql://money-tracker-username.turso.io`).
4. Generate and copy an Auth Token from the Turso dashboard.

### Step 2: Configure Environment Variables
Create a `.env` file in the root directory:
```env
BOT_TOKEN=your_telegram_bot_token_from_botfather
TURSO_DATABASE_URL=libsql://money-tracker-username.turso.io
TURSO_AUTH_TOKEN=your_turso_auth_token
PORT=8080
```

### Step 3: Run Locally (Optional)
1. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
2. Run the bot:
   ```bash
   python bot.py
   ```

### Step 4: Deploy on Render
1. Push your repository to GitHub.
2. Go to [render.com](https://render.com) and log in.
3. Click **New +** → **Web Service**.
4. Link your GitHub repository.
5. Configure the service settings:
   - **Name**: `telegram-money-tracker`
   - **Runtime**: `Python 3`
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `python bot.py`
   - **Instance Type**: `Free`
6. Add the environment variables:
   - `BOT_TOKEN`
   - `TURSO_DATABASE_URL`
   - `TURSO_AUTH_TOKEN`
7. Click **Deploy Web Service**.
