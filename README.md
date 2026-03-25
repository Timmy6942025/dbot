# dbot

A Discord bot powered by an LLM. It sees every message, decides on its own whether to respond, and remembers things about the people it talks to.

## How it works

Every message in the channel goes to the LLM. The model decides whether to respond, react, or just observe — like a human reading chat without typing. If you @mention the bot, it will always respond.

```
message arrives → recent chat + memories → ONE LLM call → respond / react / observe
```

No hardcoded rules about when to speak. The model decides.

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
├── personality.md   who the bot is, how it talks
├── system.md        the decision prompt (personality injected automatically)
└── context.md       what the LLM sees each message (chat, memories, notes)
```

Edit `personality.md` to change the bot's voice, interests, and style. Variables use `${variable}` syntax.

## CLI reference

```
python bot.py --setup                interactive setup wizard
python bot.py                        run from .env
python bot.py --token X --provider openrouter --api-key Y
python bot.py --help                 show all options
```

## Configuration

Only `DISCORD_TOKEN`, `LLM_PROVIDER`, and the matching API key are required. Everything else has sane defaults.

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
| `HISTORY_LIMIT` | `20` | Recent messages fed to the LLM each call |
| `IGNORE_PREFIXES` | `!,/,.` | Messages starting with these are skipped |
| `REPLY_MIN_DELAY_SECONDS` | `1.0` | Min delay before replying |
| `REPLY_MAX_DELAY_SECONDS` | `8.0` | Max delay before replying |
| `TYPING_CHARS_PER_SECOND` | `18.0` | Typing indicator speed |

## Testing

```bash
pip install pytest pytest-asyncio
python -m pytest test_bot.py -v
```

## License

MIT
