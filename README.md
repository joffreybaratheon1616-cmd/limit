# Onyx Limit

Professional Telegram OTP rate-limit tool with time-based access control.

## Features

- Private chat: just send `+919876543210`
- Group: `/spam +919876543210`
- `/stop` or `/stop +number`
- Any FloodWait (even 1 minute) stops the job and reports the exact wait time
- Permanent threshold set to **7 hours**
- Owner-only access control:
  - `/adduser <user_id> <days>`
  - `/removeuser <user_id>`
  - `/showuser`
- Non-authorized users are told to contact the owner (username in bot bio)

## Setup

1. Clone / download
2. Copy `.env.example` → `.env` and fill:
   - `BOT_TOKEN` → from @BotFather
   - `API_ID` + `API_HASH` → from https://my.telegram.org
   - `OWNER_ID` → your Telegram numeric user ID
3. Install:

```bash
pip install -r requirements.txt
```

4. Run:

```bash
python main.py
```

## Owner Commands

| Command | Description |
|---------|-------------|
| `/adduser 123456789 30` | Grant 30 days access |
| `/removeuser 123456789` | Revoke access |
| `/showuser` | List all users + days remaining |

Access data is stored in `access.json` next to the bot.
