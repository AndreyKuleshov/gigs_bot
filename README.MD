# Gigs Bot

Telegram bot for managing Google Calendar. Supports two modes:

- **Button mode** — deterministic FSM navigation via inline keyboards
- **AI mode** — free-text input processed by Gemini with calendar tool calls

CRUD: create, read, update, delete calendar events.

---

## Stack

| Layer | Tech |
|---|---|
| Bot | aiogram 3.x (long-polling) |
| Backend | FastAPI + uvicorn |
| Database | PostgreSQL + SQLAlchemy 2 async |
| Cache / FSM | Redis |
| AI | Gemini (google-genai SDK) |
| Calendar | Google Calendar API v3 |

---

## Project Structure

```
app/
├── core/          config, token encryption (Fernet)
├── db/            SQLAlchemy engine, ORM models
├── cache/         Redis client factory
├── services/      auth, calendar, Gemini agent
├── api/           FastAPI app + routers (OAuth callback, events CRUD)
└── bot/           aiogram handlers, FSM states, keyboards, middleware
main.py            entry point — runs bot + API in one asyncio loop
```

---

## Getting tokens and credentials

### 1. Telegram bot token

1. Open Telegram and search for **@BotFather**
2. Send `/newbot`, follow the prompts (pick a name and username)
3. BotFather replies with a token like `7123456789:AAH...`
4. Copy it → `TELEGRAM_BOT_TOKEN`

---

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

---

### 3. Gemini API key

1. Go to [Google AI Studio](https://aistudio.google.com/app/apikey)
2. Click **Create API key**
3. Copy the key → `GEMINI_API_KEY`

Default model is `gemini-1.5-flash` (fast, cheap). Change `GEMINI_MODEL` to
`gemini-1.5-pro` or `gemini-2.0-flash` if you need better reasoning.

---

### 4. Fernet encryption key

Used to encrypt Google OAuth tokens before storing them in PostgreSQL.
Generate once — **do not change it after first use** (changing it invalidates all stored tokens):

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
No manual activation needed — prefix subsequent commands with `uv run`.

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

Edit `.env` and fill in all values:

```dotenv
TELEGRAM_BOT_TOKEN=7123456789:AAH...
GOOGLE_CLIENT_ID=123456789-abc.apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=GOCSPX-...
GOOGLE_REDIRECT_URI=http://localhost:8000/auth/google/callback
GEMINI_API_KEY=AIzaSy...
GEMINI_MODEL=gemini-1.5-flash
DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/gigs_bot
REDIS_URL=redis://localhost:6379/0
FERNET_KEY=<output of the generate command above>
API_HOST=0.0.0.0
API_PORT=8000
DEBUG=false
```

### 3. Start PostgreSQL and Redis

```bash
docker compose up -d
```

Postgres is available on port `5432`, Redis on `6379`.

### 4. Run the bot

```bash
uv run python main.py
```

Database tables are created automatically on first start.
FastAPI interactive docs: [http://localhost:8000/docs](http://localhost:8000/docs)

---

## Connecting a Google account

1. Open the bot in Telegram and send `/start`
2. Send `/auth` — the bot sends an OAuth authorisation link
3. Open the link in a browser, sign in with Google, grant calendar access
4. You are redirected back to `GOOGLE_REDIRECT_URI` (handled by FastAPI)
5. Done — the bot is now ready to manage your calendar

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
- Store `FERNET_KEY` in a secret manager — losing it makes all stored tokens unreadable
- Use a process manager (systemd, Docker, supervisord) to keep the process alive
- For higher resilience, run the bot and API as separate processes
