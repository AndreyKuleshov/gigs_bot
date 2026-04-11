# Gigs Bot

Telegram bot for managing Google Calendar. Supports two interaction modes:

- **Button mode** — step-by-step FSM navigation via inline keyboards (create, list, update, delete events)
- **AI mode** — free-text input processed by OpenAI with automatic calendar tool calls; mutating actions (create/update/delete) require user confirmation before execution

### Features

- Full CRUD for Google Calendar events (timed and all-day)
- Calendar selection — choose which Google calendar to work with
- Timezone selection — pick from popular timezones or enter a custom IANA zone
- OAuth2 flow — connect your Google account directly from Telegram
- Scheduled reminders via cron expression
- Web search tool available in AI mode (DuckDuckGo)
- Token encryption at rest (Fernet)
- Proxy support for restricted environments (PythonAnywhere)
- Global error handler — user-facing error messages in chat

---

## Stack

| Layer | Tech |
|---|---|
| Bot | aiogram 3.x (long-polling / webhook) |
| Backend | FastAPI + uvicorn |
| Database | SQLite (default) or PostgreSQL + SQLAlchemy 2 async |
| AI | OpenAI function calling (gpt-4o-mini by default) |
| Calendar | Google Calendar API v3 |
| Encryption | cryptography (Fernet) |

---

## Project Structure

```
app/
├── core/          config, token encryption (Fernet)
├── db/            SQLAlchemy engine, ORM models, auto-migration
├── services/      auth, calendar, AI agent, reminders
├── api/           FastAPI app + routers (OAuth callback, events CRUD)
└── bot/           aiogram handlers, FSM states, keyboards
    └── handlers/  common, button_mode, text_mode (AI)
main.py            entry point — runs bot + API
tests/             unit tests (pytest + pytest-asyncio)
```

---

## Getting tokens and credentials

### 1. Telegram bot token

1. Open Telegram and search for **@BotFather**
2. Send `/newbot`, follow the prompts (pick a name and username)
3. BotFather replies with a token like `7123456789:AAH...`
4. Copy it → `TELEGRAM_BOT_TOKEN`

### 2. Google OAuth2 credentials

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create a project (or select an existing one)
3. **APIs & Services → Enable APIs** — enable **Google Calendar API**
4. **APIs & Services → Credentials → Create Credentials → OAuth client ID**
   - Application type: **Web application**
   - Authorised redirect URIs — add:
     ```
     http://localhost:8000/auth/google/callback
     ```
     Use your public domain instead of `localhost` for production.
5. Download or copy the values:
   - `Client ID` → `GOOGLE_CLIENT_ID`
   - `Client secret` → `GOOGLE_CLIENT_SECRET`
6. **APIs & Services → OAuth consent screen**
   - Set status to **Testing**, add your Google account as a test user

> `GOOGLE_REDIRECT_URI` in `.env` must exactly match one of the URIs registered in step 4.

### 3. OpenAI API key

1. Go to [platform.openai.com/api-keys](https://platform.openai.com/api-keys)
2. Click **Create new secret key**
3. Copy the key → `OPENAI_API_KEY`

### 4. Fernet encryption key

Used to encrypt Google OAuth tokens before storing them in the database.
Generate once — **do not change it after first use** (existing tokens become unreadable):

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Copy the output → `FERNET_KEY`

---

## Setup

### 1. Clone and install dependencies

With [uv](https://github.com/astral-sh/uv) (recommended):

```bash
git clone <repo>
cd gigs_bot
uv sync
```

`uv sync` creates the virtualenv and installs all dependencies automatically.

<details>
<summary>Without uv (plain pip)</summary>

```bash
git clone <repo>
cd gigs_bot
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e .
```

</details>

### 2. Configure environment variables

```bash
cp .env.example .env
```

Edit `.env` and fill in all values (see `.env.example` for descriptions).

### 3. Run the bot

```bash
uv run python main.py
```

Database tables are created automatically on first start.
FastAPI interactive docs: [http://localhost:8000/docs](http://localhost:8000/docs)

---

## Running tests

Install dev dependencies:

```bash
uv sync --extra dev
```

Run the full test suite:

```bash
uv run pytest
```

Run with verbose output:

```bash
uv run pytest -v
```

Run a specific test file:

```bash
uv run pytest tests/test_ai_agent.py
```

Tests use an in-memory SQLite database and fake credentials — no external services required.

---

## Connecting a Google account

1. Open the bot in Telegram and send `/start`
2. Send `/auth` — the bot sends an OAuth authorisation link
3. Open the link in a browser, sign in with Google, grant calendar access
4. You are redirected back to `GOOGLE_REDIRECT_URI` (handled by FastAPI)
5. Done — select a calendar and timezone from the menu, then start managing events

---

## Bot commands

| Command | Description |
|---|---|
| `/start` | Show main menu |
| `/auth` | Connect Google account |
| `/menu` | Return to main menu (clears FSM state) |
| `/disconnect` | Remove stored Google tokens |

Switch between **Button mode** and **AI mode** from the main menu at any time.

---

## API endpoints

All event endpoints require the `X-Telegram-User-Id` header (your Telegram numeric ID).

| Method | Path | Description |
|---|---|---|
| `GET` | `/auth/google` | Start OAuth flow (`?telegram_user_id=`) |
| `GET` | `/auth/google/callback` | OAuth redirect handler |
| `GET` | `/events/` | List upcoming events |
| `POST` | `/events/` | Create event |
| `PUT` | `/events/{event_id}` | Update event |
| `DELETE` | `/events/{event_id}` | Delete event |
| `GET` | `/health` | Health check |

Interactive docs: [http://localhost:8000/docs](http://localhost:8000/docs)

---

## Production notes

- Replace `http://localhost:8000` with your public domain everywhere (`.env` and Google Cloud Console redirect URIs)
- Set `DEBUG=false`
- For PythonAnywhere: set `WEBHOOK_URL` and `WEBHOOK_SECRET`, configure `PROXY_URL` if needed
- Store `FERNET_KEY` in a secret manager — losing it makes all stored tokens unreadable
- Use a process manager (systemd, Docker, supervisord) to keep the process alive
