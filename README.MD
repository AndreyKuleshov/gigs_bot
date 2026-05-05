# GIGs Bot

Telegram bot for managing Google Calendar events. Built as a university project demonstrating integration of multiple APIs, finite state machines, AI-powered natural language processing, and secure OAuth2 authentication.

The bot supports two interaction modes:

- **Button mode** — deterministic step-by-step navigation via inline keyboards and FSM (finite state machine) states
- **AI mode** — free-text input processed by OpenAI with automatic Google Calendar tool calls; mutating actions (create/update/delete) require explicit user confirmation

---

## Features

- Full CRUD for Google Calendar events (timed and all-day)
- Dual UI modes (button-driven FSM and free-text AI agent)
- Calendar selection — choose which Google calendar to work with
- Timezone selection — pick from popular timezones or enter a custom IANA zone
- Google OAuth2 with PKCE — connect your Google account directly from Telegram
- Scheduled event reminders via configurable cron expression
- Daily morning digest — a personalised greeting + today's events (or an
  LLM-generated upbeat "no events" phrase), delivered at the user's local
  `DAILY_DIGEST_HOUR`, scheduled by `DAILY_DIGEST_CRON` (default hourly)
- AI-powered event discovery — ask "куда сходить на выходных?" / "any
  stand-up this weekend?" and the agent searches the web for concerts,
  parties, festivals, exhibitions etc. in the user's city, returns 3-5
  options with ticket links, and offers to add picks to the calendar
- AI-powered web search (DuckDuckGo) and image search for event details
- Token encryption at rest using Fernet symmetric encryption
- Optional proxy support for restricted networks (`PROXY_URL` env)
- Global error handler with user-facing error messages
- Automatic retry with exponential backoff for transient API/proxy errors
- Overnight event handling (e.g. 20:00–00:00 automatically spans to next day)

---

## Technology Stack

| Layer | Technology | Purpose |
|---|---|---|
| Bot framework | aiogram 3.x | Telegram Bot API, FSM, inline keyboards, middleware |
| Web framework | FastAPI + uvicorn | REST API, OAuth callback, webhook endpoint |
| Database | SQLAlchemy 2 async + aiosqlite | ORM, async sessions, auto-migration |
| AI | OpenAI API (gpt-4o-mini) | Function calling, natural language processing |
| Calendar | Google Calendar API v3 | Event CRUD, calendar listing |
| Search | ddgs (DuckDuckGo) | Web search, image search |
| Encryption | cryptography (Fernet) | Token encryption at rest |
| Auth | Google OAuth2 + PKCE | Secure authorization flow |
| Package manager | uv | Fast dependency management |
| Linting | ruff + basedpyright | Code quality, type checking |
| Testing | pytest + pytest-asyncio + pytest-cov | Unit tests with branch coverage |
| Pre-commit | ruff, basedpyright, pre-commit-hooks (detect-private-key etc.) | Hooks gate every commit |

---

## Architecture

### Project Structure

```
gigs_bot/
├── app/
│   ├── core/
│   │   ├── config.py          # Pydantic Settings — loads .env, defines all configuration
│   │   └── security.py        # Fernet encrypt/decrypt for Google OAuth tokens
│   ├── db/
│   │   ├── base.py            # SQLAlchemy async engine, session factory, auto-migration
│   │   └── models.py          # ORM models: User, OAuthState
│   ├── services/
│   │   ├── auth_service.py    # Google OAuth2 flow, token management, user CRUD
│   │   ├── calendar_service.py # Google Calendar API wrapper with retry logic
│   │   ├── ai_agent.py        # OpenAI LLM agent with function calling (7 tools)
│   │   └── reminder_service.py # Reminders + cron-driven daily morning digest
│   ├── api/
│   │   ├── app.py             # FastAPI application factory with lifespan management
│   │   ├── deps.py            # FastAPI dependencies (auth header extraction)
│   │   └── routers/
│   │       ├── auth.py        # OAuth endpoints: /auth/google, /auth/google/callback
│   │       └── events.py      # REST API: GET/POST/PUT/DELETE /events
│   └── bot/
│       ├── setup.py           # Bot + Dispatcher factory, proxy session, global error handler
│       ├── states.py          # FSM state groups (6 state machines)
│       ├── keyboards.py       # Inline keyboard builders (menus, calendars, timezones)
│       ├── polling.py         # Long-polling runner (dev mode)
│       ├── scheduler.py       # Cron-driven schedulers: reminders + daily digest
│       ├── middlewares/
│       │   ├── db_session.py  # Injects async DB session into every update
│       │   └── user_sync.py   # Upserts Telegram username / full_name on every update
│       └── handlers/
│           ├── common.py      # /start, /auth, /menu, /disconnect, timezone selection
│           ├── button_mode.py # FSM-driven CRUD via inline keyboards
│           └── text_mode.py   # AI agent message handler with markdown→HTML conversion
├── tests/                     # Unit test suite (pytest)
├── main.py                    # Entry point: FastAPI + bot (polling or webhook)
├── Dockerfile                 # Container build for Coolify / any Docker host
└── pyproject.toml             # Project metadata, dependencies, tool configuration
```

### Database Schema

**Table: `users`**

| Column | Type | Description |
|---|---|---|
| `id` | BigInteger (PK) | Telegram user ID |
| `username` | String(255) | Telegram @username (kept fresh by `UserSyncMiddleware`) |
| `full_name` | String(512) | Display name (used in the daily greeting) |
| `google_tokens_encrypted` | Text | Fernet-encrypted JSON blob of OAuth tokens |
| `timezone` | String(50) | IANA timezone (default: `"UTC"`) |
| `selected_calendar_id` | String(255) | Google Calendar ID |
| `selected_calendar_name` | String(255) | Display name of selected calendar |
| `last_daily_sent_date` | Date | Idempotency guard for the daily digest scheduler |
| `created_at` | DateTime | Row creation timestamp |
| `updated_at` | DateTime | Last update timestamp |

**Table: `oauth_states`**

| Column | Type | Description |
|---|---|---|
| `state` | String(64, PK) | Random PKCE state token |
| `telegram_user_id` | BigInteger | Links state to user |
| `code_verifier` | String(256) | PKCE code verifier |
| `expires_at` | DateTime | Expiry (10 min TTL) |

### FSM State Machines

The bot uses aiogram's finite state machine (FSM) to manage multi-step interactions:

| State Group | States | Purpose |
|---|---|---|
| `CreateEventFSM` | 7 states | Collect title → start date → start time → end time → end date → description → confirm |
| `UpdateEventFSM` | 4 states | Select event → select field → input value → confirm |
| `DeleteEventFSM` | 2 states | Select event → confirm deletion |
| `SelectCalendarFSM` | 1 state | Pick a Google Calendar from the user's list |
| `AIConfirmFSM` | 1 state | Hold pending mutating action awaiting user confirmation |
| `SetTimezoneFSM` | 1 state | Pick or enter IANA timezone |

---

## How It Works

### Request Flow (Button Mode)

```
User taps button in Telegram
  → Telegram sends callback_query to webhook/polling
    → aiogram Dispatcher routes to button_mode handler
      → FSM checks current state, advances to next step
        → On confirm: calls calendar_service (Google Calendar API)
          → Google Calendar returns result
            → Handler sends response to user via Telegram
```

1. User interacts with inline keyboard buttons
2. Telegram delivers the callback to the bot (via webhook in production, long-polling in dev)
3. `DbSessionMiddleware` injects an async database session
4. The Dispatcher matches the update to a router in priority order: `common` → `button_mode` → `text_mode`
5. The handler checks FSM state and either collects input or executes the action
6. `calendar_service` makes the Google Calendar API call with automatic retry on transient errors (429, 503)
7. The response is sent back to the user with an updated inline keyboard

### Request Flow (AI Mode)

```
User sends free-text message
  → text_mode handler receives it
    → ai_agent.process_message() called
      → System prompt built (date, timezone, language, rules)
        → OpenAI chat.completions.create() with 6 tool definitions
          → Model returns tool_calls (e.g. read_events, web_search)
            → Tools executed locally, results fed back to model
              → Loop continues (max 8 rounds) until model produces final text
                → If mutating action: confirmation gate → user approves → execute
                  → Response cleaned (markdown → Telegram HTML) and sent to user
```

1. User sends a plain text message (e.g. "create an event tomorrow at 3pm called Meeting")
2. `handle_free_text()` sends a "Thinking..." indicator
3. `AIAgent.process_message()` builds a system prompt with:
   - Current date and time in user's timezone
   - Detected language (Russian or English, based on character analysis)
   - Strict formatting rules (Telegram HTML only)
   - Tool usage instructions
4. OpenAI receives the message with 7 tool definitions:
   - `read_events` — list calendar events with time/query filters
   - `create_event` — create a new event
   - `update_event` — modify an existing event (requires event ID from read_events)
   - `delete_event` — remove an event (requires event ID from read_events)
   - `get_date_range` — resolve relative periods (this_weekend, next_week, …) into ISO bounds in the user's tz
   - `web_search` — DuckDuckGo text search (English queries for best coverage)
   - `find_event_image` — DuckDuckGo image search

   Supported query flavours include:
   - Calendar CRUD: "create an event tomorrow at 3pm called Meeting"
   - Info lookup on an existing event: "найди информацию про концерт Skillet" → read_events → web_search with the calendar date → update_event to persist venue/tickets
   - **Event discovery**: "куда сходить на выходных?", "any stand-up this weekend?" → get_date_range → web_search using the user's city (derived from their IANA timezone) → 3-5 options with ticket links → user picks → create_event on confirm
5. The model may call multiple tools across up to 8 rounds
6. **Confirmation gate**: mutating tools (create/update/delete) are intercepted — the action is stored in FSM state, and the user sees a confirm/cancel keyboard
7. On confirmation: `execute_confirmed_action()` runs the calendar tool directly
8. Response text is cleaned: markdown converted to Telegram HTML, image references stripped

### OAuth2 Authentication Flow

```
User sends /auth
  → Bot generates OAuth URL with PKCE state
    → User clicks link, signs in with Google, grants calendar access
      → Google redirects to /auth/google/callback with authorization code
        → Backend exchanges code for tokens
          → Tokens encrypted with Fernet and stored in DB
            → User's timezone fetched from Google Calendar and saved
```

1. `/auth` command triggers `auth_service.get_auth_url(user_id)`
2. A random PKCE state token is generated and stored in `oauth_states` table (10 min TTL)
3. User is redirected to Google's consent screen
4. After approval, Google redirects to `/auth/google/callback` with a code
5. The code is exchanged for access + refresh tokens
6. Tokens are serialized to JSON, encrypted with Fernet, and stored in `users.google_tokens_encrypted`
7. User's Google Calendar timezone is fetched and saved to `users.timezone`
8. On subsequent requests, tokens are decrypted and refreshed automatically if expired

### Token Security

- Google OAuth tokens are **never stored in plaintext**
- `security.py` uses `cryptography.fernet.Fernet` with a symmetric key (`FERNET_KEY`)
- The encrypted blob contains: access token, refresh token, token URI, client credentials, scopes, expiry
- Losing the `FERNET_KEY` makes all stored tokens permanently unreadable

---

## Getting Tokens and Credentials

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

Default model is `gpt-4o-mini` (fast, cost-effective). Change `OPENAI_MODEL` to `gpt-4o` for better reasoning.

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

Edit `.env` and fill in all values:

| Variable | Required | Description |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | Yes | Bot token from @BotFather |
| `GOOGLE_CLIENT_ID` | Yes | OAuth2 client ID |
| `GOOGLE_CLIENT_SECRET` | Yes | OAuth2 client secret |
| `GOOGLE_REDIRECT_URI` | Yes | OAuth callback URL |
| `OPENAI_API_KEY` | Yes | OpenAI API key |
| `FERNET_KEY` | Yes | Symmetric encryption key for tokens |
| `DATABASE_URL` | No | Default: `sqlite+aiosqlite:///./gigs_bot.db` |
| `OPENAI_MODEL` | No | Default: `gpt-4o-mini` |
| `WEBHOOK_URL` | No | Set for production webhook mode |
| `WEBHOOK_SECRET` | No | Validates incoming webhook requests |
| `PROXY_URL` | No | HTTP proxy for restricted networks |
| `REMINDER_CRON` | No | Cron expression for reminders (e.g. `0 9 * * *`) |
| `DAILY_DIGEST_ENABLED` | No | Default: `false`. Set to `true` to turn on the morning digest |
| `DAILY_DIGEST_HOUR` | No | Default: `9`. Local hour (per-user tz) at which the digest is sent |
| `DAILY_DIGEST_CRON` | No | Default: `0 * * * *`. Cron for the digest tick — fires this often, sends to users whose local hour matches `DAILY_DIGEST_HOUR` and weren't sent today yet. Tighten to `*/15 * * * *` if you have users in half-hour-offset tzs (Kolkata, Iran, Adelaide, Newfoundland) |
| `API_HOST` | No | Default: `0.0.0.0` |
| `API_PORT` | No | Default: `8000` |
| `DEBUG` | No | Default: `false` |

### 3. Run the bot

```bash
uv run python main.py
```

- Database tables are created automatically on first start
- If `WEBHOOK_URL` is empty: runs in long-polling mode (for local development)
- If `WEBHOOK_URL` is set: runs in webhook mode (for production)
- FastAPI interactive docs: [http://localhost:8000/docs](http://localhost:8000/docs)

---

## Testing

### Install dev dependencies

```bash
uv sync --extra dev
```

This installs `pytest`, `pytest-asyncio`, `pytest-mock`, `pytest-cov`, `ruff`, `basedpyright`, and `pre-commit`.

### Run the full test suite

```bash
uv run pytest
```

### Run with verbose output

```bash
uv run pytest -v
```

### Run a specific test file

```bash
uv run pytest tests/test_ai_agent.py
```

### Run a specific test class or method

```bash
uv run pytest tests/test_ai_agent.py::TestExecuteTool::test_mutating_tool_intercepted
```

### Run with coverage

```bash
uv run pytest --cov=app --cov-report=term-missing
```

### Test files and what they cover

| File | Description |
|---|---|
| `tests/test_config.py` | Settings defaults and Google scopes |
| `tests/test_states.py` | All FSM state groups are defined and accessible |
| `tests/test_keyboards.py` | All keyboard builders: main menu, confirm, back, timezone, calendar list |
| `tests/test_security.py` | Fernet encrypt/decrypt roundtrip |
| `tests/test_calendar_service.py` | Pydantic event models, `_parse_dt` for ISO/date parsing |
| `tests/test_ai_agent.py` | Tool interception, timezone-fixing, `_resolve_date_range`, `_city_from_tz`, system-prompt rendering |
| `tests/test_daily_digest.py` | `send_daily_digest_to_user`: gating, dedup, force, name fallback via `bot.get_chat`, LLM empty-day fallback |
| `tests/test_user_sync_middleware.py` | Middleware extracts `from_user` from each update kind and upserts |
| `tests/test_button_mode_auth.py` | `_handle_auth_failure` clears state, renders OAuth-link prompt; `_is_auth_error` classifier |
| `tests/test_text_debounce.py` | 3-msg burst → single AI call, debounce=0 inline path, multi-user buffers, auth short-circuit |
| `tests/test_send_reminders.py` | `_remind_user` happy path, no-creds/no-events early returns, invalid tz → UTC; `send_reminders` swallows per-user exceptions |
| `tests/test_scheduler_guards.py` | `start_scheduler` / `start_daily_digest_scheduler` no-op on empty cron; one-iteration loop |
| `tests/test_app_factory.py` | `create_app` returns FastAPI with `/health`, `/webhook/telegram`, `/auth/google*` routes |

### Test environment

Tests use mock credentials and an in-memory SQLite database. No external services (Google, OpenAI, Telegram) are called during testing. The test configuration is set up in `tests/conftest.py`:

```python
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "000000000:AAFakeTokenForTests")
os.environ.setdefault("GOOGLE_CLIENT_ID", "fake-client-id")
os.environ.setdefault("GOOGLE_CLIENT_SECRET", "fake-client-secret")
os.environ.setdefault("FERNET_KEY", "uN5G7QOJHAoEefkLBrumiB5jm19dJI7TECz878jB7A0=")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("OPENAI_API_KEY", "sk-fake-key-for-tests")
```

### Linting and type checking

```bash
uv run ruff check .          # Lint
uv run ruff format .         # Auto-format
uv run basedpyright          # Type checking
```

### Pre-commit

Hooks gate every commit with `ruff`, `ruff-format`, `basedpyright`, plus
`pre-commit-hooks` for `detect-private-key`, `check-added-large-files`,
`check-yaml/toml`, `check-merge-conflict`, `end-of-file-fixer`, and
`trailing-whitespace`.

```bash
uv run pre-commit install          # one-time, wires .git/hooks
uv run pre-commit run --all-files  # run all hooks ad-hoc
```

---

## Bot Commands

| Command | Description |
|---|---|
| `/start` | Show main menu with calendar and timezone info |
| `/auth` | Connect Google account via OAuth2 |
| `/menu` | Return to main menu (clears any active FSM state) |
| `/disconnect` | Remove stored Google tokens and disconnect account |

Switch between **Button mode** and **AI mode** from the main menu at any time.

### Main Menu Layout

When a calendar is selected, the menu shows:
- **List events** / **Create event** / **Update event** / **Delete event** buttons
- Current calendar name (tap to change)
- Current timezone (tap to change)
- Mode switch (Button / AI)

When no calendar is selected, only the calendar selection and timezone buttons are shown.

---

## REST API Endpoints

All event endpoints require the `X-Telegram-User-Id` header with the Telegram numeric user ID.

| Method | Path | Description | Status |
|---|---|---|---|
| `GET` | `/auth/google?telegram_user_id=ID` | Initiate OAuth flow | 302 redirect |
| `GET` | `/auth/google/callback` | OAuth callback handler | 200 HTML |
| `GET` | `/events/` | List upcoming events | 200 |
| `POST` | `/events/` | Create event | 201 |
| `PUT` | `/events/{event_id}` | Update event | 200 |
| `DELETE` | `/events/{event_id}` | Delete event | 204 |
| `GET` | `/health` | Health check | 200 |
| `POST` | `/webhook/telegram` | Telegram webhook receiver | 200 |

Interactive docs: [http://localhost:8000/docs](http://localhost:8000/docs)

---

## Deployment

### Local development

```bash
uv run python main.py
```

Runs in long-polling mode (no webhook needed). Bot pulls updates from Telegram API.

### Docker host (Coolify / Fly.io / Render / plain Docker)

1. Build with the included `Dockerfile` (multi-stage, python:3.11-slim + uv).
2. Mount a persistent volume at `/data` for the SQLite DB.
3. Provide env vars (see table above). Set `WEBHOOK_URL` to your public
   HTTPS URL and update `GOOGLE_REDIRECT_URI` in Google Cloud Console.
4. Reminders and the daily morning digest run in-process via FastAPI's
   lifespan — enable with `REMINDER_CRON` and `DAILY_DIGEST_ENABLED` in env.

---

## Production Notes

- Replace `http://localhost:8000` with your public domain in `.env` and Google Cloud Console redirect URIs
- Set `DEBUG=false`
- Store `FERNET_KEY` securely — losing it makes all stored tokens permanently unreadable
- If a proxy is required (`PROXY_URL` env): the bot retries up to 5 times with exponential backoff on proxy failures, and `sendPhoto` is auto-degraded to a clickable link (the proxy chokes on multipart photo uploads).
- Use a process manager (systemd, Docker, supervisord) to keep the process alive on self-hosted deployments
