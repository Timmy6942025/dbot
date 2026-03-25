# dbot

A Discord bot that decides when to speak. It reads the room, remembers people, and only jumps in when it actually has something to say.

## Features

- **Selective participation** — uses an LLM to decide whether to ignore, reply, react, or defer
- **Episodic memory** — remembers conversations per channel with salience scoring
- **Relational memory** — tracks engagement and trust scores per user
- **Deferred follow-ups** — can wait and respond later if nobody else does
- **Rate limiting** — won't spam channels; respects cooldowns and message windows
- **Editable prompts** — all system prompts are plain markdown files you can customize
- **Multiple providers** — works with OpenAI, OpenRouter, or Kilo Gateway

## Quick start

```bash
pip install -r requirements.txt
python bot.py --setup
```

The setup wizard asks for your Discord token, LLM provider, and API key. That's it.

## Manual setup

Copy `.env.example` to `.env` and fill in the three required values:

```
DISCORD_TOKEN=your_discord_bot_token
LLM_PROVIDER=openrouter
OPENROUTER_API_KEY=sk-or-...
```

Then run:

```bash
python bot.py
```

## LLM providers

| Provider | Env var | Default model | Cost |
|----------|---------|---------------|------|
| `openrouter` | `OPENROUTER_API_KEY` | `arcee-ai/trinity-large-preview:free` | Free |
| `kilo` | `KILO_API_KEY` | `stepfun/step-3.5-flash:free` | Free |
| `openai` | `OPENAI_API_KEY` | `gpt-4o-mini` | Paid |

Override the model with `--model` or the provider-specific env var.

## Customizing prompts

All prompts live in `prompts/` as markdown files:

```
prompts/
├── personality.md        who the bot is, how it talks
├── decide_system.md      when to respond + JSON output format
├── decide_user.md        context injected per message
├── deferred_system.md    follow-up logic + JSON output format
└── deferred_user.md      context injected for follow-ups
```

Edit `personality.md` to change the bot's voice, interests, and style. The other files control decision logic — be careful changing the JSON schemas.

Variables use `${variable}` syntax. See each file for available placeholders.

## CLI reference

```
python bot.py --setup                interactive setup wizard
python bot.py                        run from .env
python bot.py --token X --provider openrouter --api-key Y
python bot.py --help                 show all options
```

## Configuration

All settings have defaults. Only `DISCORD_TOKEN`, `LLM_PROVIDER`, and the matching API key are required.

| Variable | Default | Description |
|----------|---------|-------------|
| `DISCORD_TOKEN` | — | Discord bot token |
| `LLM_PROVIDER` | `openai` | `openai`, `openrouter`, or `kilo` |
| `OPENAI_API_KEY` | — | OpenAI API key |
| `OPENROUTER_API_KEY` | — | OpenRouter API key |
| `KILO_API_KEY` | — | Kilo Gateway API key |
| `BOT_NAME` | `dbot` | Bot display name |
| `DATABASE_PATH` | `./dbot.sqlite3` | SQLite database path |
| `LOG_LEVEL` | `INFO` | Logging level |
| `HISTORY_LIMIT` | `20` | Messages of context to fetch |
| `CHANNEL_COOLDOWN_SECONDS` | `30` | Min seconds between decisions per channel |
| `SALIENCE_WAKE_THRESHOLD` | `0.65` | Min score to respond without a tag |
| `MAX_MESSAGES_PER_WINDOW` | `3` | Max bot messages per rate window |
| `RATE_LIMIT_WINDOW_SECONDS` | `300` | Rate limit window (5 min) |
| `MAX_CONSECUTIVE_MESSAGES` | `2` | Max consecutive bot messages |
| `REPLY_MIN_DELAY_SECONDS` | `1.0` | Min reply delay |
| `REPLY_MAX_DELAY_SECONDS` | `8.0` | Max reply delay |
| `DEFER_POLL_SECONDS` | `10` | How often to check deferred tasks |
| `MAX_DEFER_SECONDS` | `1800` | Max defer time (30 min) |
| `MIN_DEFER_SECONDS` | `30` | Min defer time |

## License

MIT
