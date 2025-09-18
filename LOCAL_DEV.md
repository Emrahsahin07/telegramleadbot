# Local Run Guide

## Quick Start

Run the bot directly via the main entry point:

```bash
python Botparsing.py
```

Requirements in `.env`:
- `API_ID`, `API_HASH`
- `LEADBOT_TOKEN`
- `OPENAI_API_KEY`
- Optional: `OPENAI_MODEL`, `AI_TIMEOUT`, `OPENAI_RPS`

Notes:
- Session files are created automatically (`bot_parser.session`, `bot_session.session`).
- Logs are written to `bot.log` and AI logs per `.context/rules.md`.
