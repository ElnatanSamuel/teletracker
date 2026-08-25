import logging
from fastapi import FastAPI
from api.webhook import query_turso, build_daily_summary, send_tg_message, main_menu_keyboard

logging.basicConfig(level=logging.INFO)

app = FastAPI()

@app.get("/api/daily_summary")
@app.get("/")
async def daily_summary():
    users = await query_turso("SELECT user_id, chat_id FROM users")
    for user_id, chat_id in users:
        try:
            report = await build_daily_summary(int(user_id))
            await send_tg_message(int(chat_id), report, main_menu_keyboard())
        except Exception as e:
            logging.error(f"Error sending summary to {user_id}: {e}")
    return {"status": "success", "count": len(users)}
