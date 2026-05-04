# Dr. Fraudsworth — Laboratory Assistant Telegram Bot

A Telegram buy bot that monitors Solana on-chain transactions (buys, sells, stakes, epoch flips, carnage events) for the Dr. Fraudsworth ecosystem tokens (CRIME, FRAUD, PROFIT) and posts alerts to a Telegram group.

## Setup

1. Copy `.env.example` to `.env` and fill in your credentials:
   ```bash
   cp .env.example .env
   ```

2. Install dependencies:
   ```bash
   pip install python-telegram-bot aiohttp matplotlib python-dotenv
   ```

3. Run the bot:
   ```bash
   python bot.py
   ```

## Files

- `bot.py` — Main bot logic: WebSocket listener, transaction parsing, Telegram command handlers
- `config.py` — All configuration constants (reads secrets from environment variables)
- `db.py` — SQLite persistence for settings, admins, images, and stats
