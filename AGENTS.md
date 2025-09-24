# Repository Guidelines

## Project Structure & Module Organization
Core runtime lives in `Botparsing.py`, wiring Telethon clients, filters, delivery, and AI helpers from `filters.py`, `delivery.py`, and `ai_utils.py`. Connection lifecycle logic sits in `connection_manager.py`, while `config.py` loads environment variables, logging, and shared clients. Persistent data lives in SQLite files (`queue.db`, `feedback.db`) and JSON configs (`categories.json`, `subscriptions.json`, `summary/summary_updated.json`). Runtime artefacts such as `bot.log`, `metrics.json`, and `bot_parser.session*` are created in the repository root.

## Build, Test, and Development Commands
Set up a virtual env and install dependencies before running the bot:
- `python -m venv .venv && source .venv/bin/activate`
- `pip install -r requirements.txt`
Start the bot with `python Botparsing.py`; add `BOT_DEBUG=1` for verbose logging. Rebuild the delivery queue schema with `python reset_db.py` if SQLite drift is suspected. Validate the keep-alive Flask endpoint via `python keep_alive.py` (defaults to port 8080).

## Coding Style & Naming Conventions
Follow idiomatic Python 3.10+ style: four-space indentation, type-hinted function signatures, and descriptive snake_case names for functions, modules, and async tasks. Prefer f-strings for formatting and route production logging through `config.logger`; reserve `print` for utilities such as `reset_db.py`. Add new triggers or offer terms in `constants.py` so they remain centrally curated.

## Testing Guidelines
Automated tests are not yet present, so rely on deterministic manual checks. Run the bot against a staging Telegram API key and monitor `bot.log` plus AI-specific logs (`ai_*.log`). For classification tweaks, seed representative messages via the queue helpers and inspect `metrics.json` to confirm counter changes, resetting the database beforehand to avoid stale state.

## Commit & Pull Request Guidelines
Existing history shows short, descriptive summaries (e.g., `обновил ai_utils.py`); keep commits focused and written in the imperative voice, in Russian or English. Reference related issues or chat IDs in the body when relevant. Pull requests should explain the user-facing effect, list manual verification steps, and attach screenshots or log excerpts. Call out configuration or schema migrations explicitly so reviewers can apply them.

## Security & Configuration Tips
Populate `.env` with `API_ID`, `API_HASH`, `LEADBOT_TOKEN`, and `OPENAI_API_KEY`; never commit secrets or `.session` files. Rotate keys after shared testing and scrub sensitive data from logs before distributing them. Keep separate `.env` copies per environment and confirm `.gitignore` coverage before adding new credential files.
