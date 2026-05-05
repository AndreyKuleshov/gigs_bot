# Миграция gigs_bot: PythonAnywhere → Contabo Cloud VPS 10 + Coolify

> **✅ COMPLETED — 2026-05-05.** Бот живёт на `https://gigs-bot.duckdns.org`,
> Contabo VPS S `161.97.89.82`, Ubuntu 24.04, Coolify v4.0.0, FastAPI
> lifespan-схедулеры работают. Telegram webhook переключён, OAuth redirect
> URI добавлен в Google Console, `gigs_bot.db` перенесён, LE-сертификат
> выдан. PA webapp пока живой как rollback safety net до ~2026-05-12;
> после этого Фаза 7 (cleanup) исполнима.

Рунбук для Claude. Разделён на фазы: **[LOCAL]** — я делаю в репо сам,
**[USER]** — требуется действие пользователя, **[REMOTE]** — SSH на сервер
(после того как пользователь его подготовит).

## Цели
1. Съехать с PA free (single-worker, CPU lock, HTTP тики).
2. Получить «home base» для будущих приложений (bot + Postgres + Redis + Mongo на одной машине).
3. FastAPI lifespan schedulers снова работают — инъекции daemon-thread и tick-endpoint'ы больше не нужны (оставим как dead-but-ok fallback).

## Целевая архитектура
- **Contabo Cloud VPS 10** (~$5–6/мес, 4 vCPU AMD EPYC, 8 ГБ RAM, 75 ГБ NVMe), Ubuntu 24.04 LTS.
- **Docker Engine + Docker Compose plugin** из официального репо.
- **Coolify** — в собственном контейнере, за своим Traefik. Берёт на себя:
  - git deploy (GitHub/GitLab)
  - `Dockerfile`-build gigs_bot
  - outbound services: Postgres, Redis, Mongo (по запросу)
  - LE-сертификаты и reverse-proxy для доменов
  - секреты / env / логи / бэкапы
- Бот — Docker-сервис в Coolify, слушает внутри контейнера `0.0.0.0:8000`, Traefik снаружи даёт HTTPS.

## Что мы НЕ делаем
- Postgres / Redis / Mongo сейчас для бота — остаётся SQLite в volume. Поднимем БД когда будут другие апп.
- Не сносим ничего на PA до успешной проверки нового деплоя.

---

## ✅ Фаза 0: [LOCAL] Подготовка кода — DONE

Цель: получить гарантированно собираемый Docker-образ и понять, чего
не хватает в конфиге.

### 0.1 Создать `Dockerfile`
Multi-stage: `python:3.11-slim` base, `uv` для установки зависимостей,
финальный образ без toolchain. Запуск: `python main.py`. Слушать на
`API_PORT=8000`, `API_HOST=0.0.0.0`.

### 0.2 Создать `.dockerignore`
Исключить: `.venv`, `__pycache__`, `.git`, `.pytest_cache`,
`gigs_bot.db`, `tests/`, `.env`, `MIGRATION_PLAN.md`, `*.md` (кроме
README.MD).

### 0.3 Проверить конфиг
- `settings.database_url` — SQLite по умолчанию, путь `./gigs_bot.db`.
  Для Docker надо, чтобы этот путь был в **volume**. Запланировать
  mount `/data` → установить `DATABASE_URL=sqlite+aiosqlite:////data/gigs_bot.db`.
- `PROXY_URL` — не нужен на Hetzner, убрать из env.
- `WEBHOOK_URL` — будет новый домен.
- `GOOGLE_REDIRECT_URI` — новый домен.

### 0.4 Smoke-test локально
```
docker build -t gigs-bot-test .
docker run --rm --env-file .env.local -p 8000:8000 gigs-bot-test
curl http://localhost:8000/health
```
`/health` должен ответить 200. Если нет — чинить до Hetzner.

### 0.5 Commit Dockerfile + .dockerignore
```
git add Dockerfile .dockerignore
git commit -m "Add Dockerfile for container deploy (Hetzner/Coolify)"
git push origin main
```

---

## ✅ Фаза 1: [USER] Contabo provisioning — DONE (IP 161.97.89.82)

### 1.1 Аккаунт
Зарегистрироваться на contabo.com. Email + пароль. Документы НЕ
просят (только карту/PayPal при заказе).

### 1.2 SSH-ключ (заранее)
```
ssh-keygen -t ed25519 -C "contabo-coolify" -f ~/.ssh/contabo_coolify
```
Сохрани содержимое `~/.ssh/contabo_coolify.pub` — впишем его на шаге
оформления заказа.

### 1.3 Заказ VPS
Открыть https://contabo.com/en/vps/ → плитка **Cloud VPS 10** →
кнопка **Configure**:
- **Region:** EU (Germany — Düsseldorf или Nürnberg).
- **Image:** Ubuntu 24.04 LTS.
- **Storage:** 75 GB NVMe (быстрее 150 GB SSD; для бота + Coolify
  + 2–3 апп с БД 75 ГБ хватит с большим запасом).
- **Period:** 1 month (можно 12 для скидки, но первый раз — месяц).
- **Add-ons:**
  - **Add SSH key** — вставить содержимое `.pub` ключа.
  - Auto-Backup (+€1.40/мес) — рекомендую включить.
  - Дополнительный IPv4 / Snapshot — пропустить.
- Оформление: карта или PayPal. Подтверждение по email.
- **Provisioning:** 1–4 часа. Дождаться письма «Your VPS is ready»
  с IP-адресом и паролем root.

### 1.4 Дать IP
Передать мне в чат публичный IPv4 (из welcome-письма Contabo).
Также сообщить — пароль root (если нужен fallback) или подтвердить,
что SSH-ключ работает. Переходим к Фазе 2.

### 1.5 Домен (параллельно)
Для HTTPS и Telegram webhook нужен домен. Варианты:
- **Есть свой домен** — в DNS провайдере добавить A-запись:
  `bot.example.com` → `<hetzner_ip>`
  (+ wildcard для Coolify dashboard: `*.example.com` → тот же IP).
- **Нет домена** — купить в namecheap/cloudflare (~$10/год) или
  использовать duckdns.org (бесплатный DDNS).
- DNS propagation — до 24 часов, обычно 5–30 минут.

---

## ✅ Фаза 2: [REMOTE] Базовая настройка сервера — DONE

Я делаю через SSH. Пользователю нужно один раз дать согласие на ввод
моих команд, либо пробросить мне ключ (не рекомендую, пусть он
выполняет у себя и пересылает output).

### 2.1 Первый login
```
ssh -i ~/.ssh/contabo_coolify root@<IP>
```
Принять host key. Если SSH-ключ не сработал и Contabo прислал
пароль — войти по паролю, добавить ключ в `/root/.ssh/authorized_keys`.

### 2.2 Обновить систему
```
apt update && apt upgrade -y
apt install -y ufw fail2ban curl git
```

### 2.3 Non-root user (для SSH)
Coolify рекомендует root для установки, но SSH из-под root лучше
отключить. Делаем пользователя, даём sudo, переносим ключ:
```
adduser --disabled-password --gecos "" greenolls
usermod -aG sudo greenolls
mkdir -p /home/greenolls/.ssh
cp ~/.ssh/authorized_keys /home/greenolls/.ssh/
chown -R greenolls:greenolls /home/greenolls/.ssh
chmod 700 /home/greenolls/.ssh
chmod 600 /home/greenolls/.ssh/authorized_keys
```
Проверить, что ключом логинится `greenolls` (не закрывая root-сессию!):
```
ssh -i ~/.ssh/contabo_coolify greenolls@<IP>
sudo -i
```

### 2.4 UFW firewall
```
ufw allow 22/tcp
ufw allow 80/tcp
ufw allow 443/tcp
ufw allow 8000/tcp    # Coolify dashboard temp — закроем позже
ufw --force enable
ufw status
```

### 2.5 SSH hardening
`/etc/ssh/sshd_config.d/99-hardening.conf`:
```
PermitRootLogin no
PasswordAuthentication no
```
`systemctl restart ssh`. **Проверить логин под greenolls до закрытия
рутовой сессии.**

### 2.6 Swap (2 ГБ — на случай пиков Coolify/Mongo)
```
fallocate -l 2G /swapfile
chmod 600 /swapfile
mkswap /swapfile
swapon /swapfile
echo '/swapfile none swap sw 0 0' >> /etc/fstab
```

---

## ✅ Фаза 3: [REMOTE] Установка Coolify — DONE (Coolify v4.0.0)

### 3.1 Одна команда
```
curl -fsSL https://cdn.coollabs.io/coolify/install.sh | bash
```
Установит Docker, Docker Compose, поднимет Coolify в контейнерах.
Занимает 3–5 минут.

### 3.2 Первый вход
```
http://<IP>:8000
```
Зарегистрировать первого админа (это будет owner). **Выбрать
сильный пароль, сохранить в менеджере.**

### 3.3 Привязать домен к Coolify dashboard
В Coolify Settings → General → Instance domain:
`coolify.example.com`. LE-сертификат подтянется автоматически, если
A-запись настроена.

После этого закрыть 8000:
```
ufw delete allow 8000/tcp
```

---

## ✅ Фаза 4: [LOCAL + USER] Deploy бота в Coolify — DONE (app uuid y1345hhcrnbhtey4y8jddkqa)

### 4.1 Подключить GitHub
Coolify → Sources → **+ New** → GitHub App. Нужно установить Coolify
GitHub App в аккаунт AndreyKuleshov, дать доступ к репо `gigs_bot`.

### 4.2 Новый проект + environment
Projects → **+ New** → `gigs` / Production.

### 4.3 Новый ресурс
Resources → **+ New** → Application → Public Git Repository (или
Private via GitHub App).
- **Repo:** `AndreyKuleshov/gigs_bot`
- **Branch:** `main`
- **Build Pack:** Dockerfile
- **Port Exposes:** 8000
- **Domain:** `bot.example.com`
- **Health check:** `/health`

### 4.4 Env vars
Вставить все переменные (спрашиваем у пользователя те, что не знаем,
или через PA API читаем его `.env`):
- `TELEGRAM_BOT_TOKEN` *(перенести)*
- `GOOGLE_CLIENT_ID` *(перенести)*
- `GOOGLE_CLIENT_SECRET` *(перенести)*
- `GOOGLE_REDIRECT_URI=https://bot.example.com/auth/google/callback`
- `OPENAI_API_KEY` *(перенести)*
- `FERNET_KEY` *(перенести — КРИТИЧНО, тот же что на PA, иначе
  токены в БД расшифровать не сможем)*
- `WEBHOOK_URL=https://bot.example.com`
- `WEBHOOK_SECRET` *(перенести или сгенерировать новый)*
- `DATABASE_URL=sqlite+aiosqlite:////data/gigs_bot.db`
- `DAILY_DIGEST_ENABLED=true`
- `DAILY_DIGEST_HOUR=9`
- **НЕ ставить** `PROXY_URL`, `REMINDER_CRON`.

### 4.5 Persistent storage
Storage → **+ Add**:
- Type: Volume mount
- Source: `gigs-bot-data`
- Target: `/data`

Это сохранит `gigs_bot.db` между деплоями.

### 4.6 Первый deploy
Нажать **Deploy**. Смотреть логи в UI. Ждать `Application is live`.

### 4.7 Проверка
```
curl https://bot.example.com/health
# -> {"status":"ok"}
```

---

## ✅ Фаза 5: [LOCAL + USER] Перенос данных — DONE (gigs_bot.db перенесён в /data volume)

### 5.1 Вытянуть gigs_bot.db с PA
У меня уже есть PA API token. Делаем:
```
curl -H "Authorization: Token <PA_TOKEN>" \
  "https://www.pythonanywhere.com/api/v0/user/greenolls/files/path/home/greenolls/gigs_bot/gigs_bot.db" \
  -o /tmp/gigs_bot.db.from_pa
```

### 5.2 Залить на Hetzner
Через Coolify UI нет прямого uploader'а. Проще через SSH:
```
scp -i ~/.ssh/contabo_coolify /tmp/gigs_bot.db.from_pa greenolls@<IP>:/tmp/
ssh -i ~/.ssh/contabo_coolify greenolls@<IP>
sudo docker ps --format '{{.ID}} {{.Names}}' | grep gigs
# найти container id бота
sudo docker cp /tmp/gigs_bot.db.from_pa <container_id>:/data/gigs_bot.db
sudo docker restart <container_id>
```

### 5.3 Sanity-check
```
curl https://bot.example.com/health
# Проверить /internal/tick-digest с правильным секретом — должен работать
```
В Telegram написать боту — должен ответить.

---

## ✅ Фаза 6: [LOCAL + USER] Переключение webhook + Google OAuth — DONE

### 6.1 Google OAuth redirect URI
[USER] Открыть Google Cloud Console → Credentials → OAuth 2.0 Client ID
→ Authorized redirect URIs → **Добавить**:
```
https://bot.example.com/auth/google/callback
```
(старый PA URI оставить пока, удалим после финальной проверки).

### 6.2 Telegram webhook
```
TOKEN=<bot_token>
SECRET=<webhook_secret>
curl -F "url=https://bot.example.com/webhook/telegram" \
     -F "secret_token=$SECRET" \
     "https://api.telegram.org/bot$TOKEN/setWebhook"
```
Должно вернуть `{"ok":true,"result":true,"description":"Webhook was set"}`.

Проверить:
```
curl "https://api.telegram.org/bot$TOKEN/getWebhookInfo"
# url поле должно быть bot.example.com
```

### 6.3 End-to-end тест
- В Telegram: `/start` — должно прийти главное меню.
- `/menu`, создать событие, «куда сходить на выходных?».
- Проверить логи Coolify — нет warnings/errors.

### 6.4 Daily digest smoke-test
```
ssh -i ~/.ssh/contabo_coolify greenolls@<IP>
sudo docker exec -it <container_id> \
  python run_daily_digest.py --user <my_tg_id> --force
```
В Telegram должно прийти daily digest.

---

## ⏳ Фаза 7: [LOCAL + USER] Cleanup — PENDING (через ~неделю наблюдения)

### 7.1 Оставить PA в работе 3–7 дней
Как fallback на случай, если что-то вылезет.

### 7.2 После периода проверки
- [USER] Contabo → проверить, что Backups включены и расписание ок.
- Удалить устаревший Google OAuth redirect URI с PA-доменом.
- PA Web tab → disable webapp.
- cron-job.org — удалить job на `/internal/tick-digest` (FastAPI
  lifespan schedulers теперь запускают digest сами).
- В репо убрать wsgi.py-хак daemon-thread и tick-endpoint'ы
  (dead code в новой архитектуре). Отдельный commit.
- Закрыть PA-аккаунт или просто перестать платить.

---

## Rollback план
Если после webhook-переключения бот ломается и быстро починить не
удаётся:
1. Переставить webhook обратно на PA:
   ```
   curl -F "url=https://greenolls.pythonanywhere.com/webhook/telegram" \
        -F "secret_token=$SECRET" \
        "https://api.telegram.org/bot$TOKEN/setWebhook"
   ```
2. PA webapp всё ещё живой (мы его не сносим до Фазы 7).
3. Google OAuth пока имеет оба redirect URI — старые flow работают.
4. Разбираемся с проблемой на Hetzner без пресса.

---

## Чек-лист того, что мне нужно от пользователя
- [ ] Contabo IP сервера (из welcome-email после провижна)
- [ ] Домен + DNS (A-запись готова) — или подтверждение использовать duckdns.org
- [ ] Доступ к репо `gigs_bot` для Coolify GitHub App
- [ ] Подтверждение env vars (те, что я не могу прочитать)
- [ ] Решение: переносим SQLite as-is, или сразу Postgres?
  (рекомендую SQLite — меньше сюрпризов с миграцией схемы)

## Таймлайн
- Фаза 0 (LOCAL prep): 30 мин (Claude сам).
- Фаза 1 (Contabo setup): 15 мин user + **до 4 ч ожидания** провижна
  (Contabo не моментальный).
- Фазы 2–3 (сервер + Coolify): 30 мин.
- Фаза 4 (deploy): 15 мин.
- Фаза 5 (данные): 10 мин.
- Фаза 6 (переключение): 10 мин + 30 мин тестирования.
- Итого активной работы: ~2 часа (без учёта ожидания провижна Contabo)
  + неделя observation до Фазы 7.
