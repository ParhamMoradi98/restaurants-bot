# PlateRoulette

Telegram bot for shared restaurant daily specials. Households invite members, digests run per-person timezone, and specials live in SQLite.

## Local run

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # set TELEGRAM_BOT_TOKEN
export $(grep -v '^#' .env | xargs)
python specials_bot.py
```

## Deploy on Railway (from GitHub)

1. Push this repo to GitHub.
2. In [Railway](https://railway.app): **New Project → Deploy from GitHub** → pick the repo.
3. Add variables (Variables tab):
   - `TELEGRAM_BOT_TOKEN` — from [@BotFather](https://t.me/BotFather)
   - `SPECIALS_TZ` — optional, default `America/Toronto`
4. Attach a **Volume** mounted at `/data` so the SQLite DB survives redeploys.  
   The bot writes to `/data/specials.db` automatically when `/data` exists (or set `SPECIALS_DB` yourself).
5. Deploy. Start command is already set in `railway.toml`: `python specials_bot.py`.
6. No public HTTP URL is required — the bot uses Telegram long polling.

### Migrating an existing local `specials.db`

After the first deploy with a volume, copy your local DB into the volume (Railway shell or a one-off upload), e.g. place it at `/data/specials.db`, then restart the service.

## Env vars

| Variable | Required | Description |
|----------|----------|-------------|
| `TELEGRAM_BOT_TOKEN` | yes | Bot token from BotFather |
| `SPECIALS_DB` | no | SQLite path (default: `/data/specials.db` on Railway, else `./specials.db`) |
| `SPECIALS_TZ` | no | Default timezone for new members |
