# OTP Bot

Telegram bot that repeatedly requests login codes for a phone number.

## Features
- Private chat: just send `+33668081139`
- Group: `/spam +33668081139`
- `/stop` or `/stop +number`
- Continues on short FloodWait (< 9 h)
- Stops permanently on long rate-limit (≥ 9 h)

## Setup

1. Clone / download
2. Create `.env` from `.env.example` and fill:
   - `BOT_TOKEN` → from @BotFather
   - `API_ID` + `API_HASH` → from https://my.telegram.org
3. Install:
   ```bash
   pip install -r requirements.txt
