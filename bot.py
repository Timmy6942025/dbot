from __future__ import annotations

import os
import re
import sys
import json
import time
import random
import logging
import asyncio
import argparse
import string
from dataclasses import dataclass, field
from typing import Any, Optional, Literal

import aiosqlite
import discord
from dotenv import load_dotenv
from openai import AsyncOpenAI

# ============================================================
# CONFIG
# ============================================================

load_dotenv()

LLM_PROVIDER = Literal["openai", "openrouter", "kilo"]

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
KILO_BASE_URL = "https://api.kilo.ai/api/gateway"


@dataclass(frozen=True)
class Config:
    discord_token: str
    llm_provider: LLM_PROVIDER
    openai_api_key: str
    openai_model: str
    openrouter_api_key: str
    openrouter_model: str
    kilo_api_key: str
    kilo_model: str
    bot_name: str
    database_path: str
    log_level: str

    history_limit: int
    ignore_prefixes: tuple[str, ...]

    reply_min_delay_seconds: float
    reply_max_delay_seconds: float
    typing_chars_per_second: float

    @staticmethod
    def load(overrides: dict[str, str] | None = None) -> "Config":
        ov = overrides or {}

        def pick(key: str, default: str) -> str:
            return ov.get(key) or os.getenv(key, default)

        def pick_int(key: str, default: int) -> int:
            raw = ov.get(key) or os.getenv(key)
            return int(raw) if raw is not None and str(raw).strip() else default

        def pick_float(key: str, default: float) -> float:
            raw = ov.get(key) or os.getenv(key)
            return float(raw) if raw is not None and str(raw).strip() else default

        provider = pick("LLM_PROVIDER", "openai")
        if provider not in ("openai", "openrouter", "kilo"):
            raise ValueError(
                f"Invalid LLM_PROVIDER '{provider}'. Must be one of: openai, openrouter, kilo"
            )

        # Route --api-key to the correct provider key
        api_key_env = {
            "openai": "OPENAI_API_KEY",
            "openrouter": "OPENROUTER_API_KEY",
            "kilo": "KILO_API_KEY",
        }
        if "api_key" in ov:
            ov[api_key_env.get(provider, "OPENAI_API_KEY")] = ov.pop("api_key")

        ignore_prefixes_raw = pick("IGNORE_PREFIXES", "!,/,.")
        ignore_prefixes = tuple(
            p.strip() for p in ignore_prefixes_raw.split(",") if p.strip()
        )

        return Config(
            discord_token=pick("DISCORD_TOKEN", ""),
            llm_provider=provider,  # type: ignore[arg-type]
            openai_api_key=pick("OPENAI_API_KEY", ""),
            openai_model=pick("OPENAI_MODEL", "gpt-4o-mini"),
            openrouter_api_key=pick("OPENROUTER_API_KEY", ""),
            openrouter_model=pick("OPENROUTER_MODEL", "arcee-ai/trinity-large-preview:free"),
            kilo_api_key=pick("KILO_API_KEY", ""),
            kilo_model=pick("KILO_MODEL", "stepfun/step-3.5-flash:free"),
            bot_name=pick("BOT_NAME", "dbot"),
            database_path=pick("DATABASE_PATH", "./dbot.sqlite3"),
            log_level=pick("LOG_LEVEL", "INFO").upper(),
            history_limit=pick_int("HISTORY_LIMIT", 20),
            ignore_prefixes=ignore_prefixes,
            reply_min_delay_seconds=pick_float("REPLY_MIN_DELAY_SECONDS", 1.0),
            reply_max_delay_seconds=pick_float("REPLY_MAX_DELAY_SECONDS", 8.0),
            typing_chars_per_second=pick_float("TYPING_CHARS_PER_SECOND", 18.0),
        )


CONFIG = Config.load()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("dbot")

# ============================================================
# DATABASE
# ============================================================

CREATE_TABLES_SQL = """
PRAGMA journal_mode=WAL;
PRAGMA busy_timeout=5000;

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    author_id TEXT,
    author_name TEXT NOT NULL,
    content TEXT NOT NULL,
    is_bot INTEGER NOT NULL DEFAULT 0,
    ts REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_messages_channel_ts
ON messages(channel_id, ts DESC);

CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id TEXT NOT NULL,
    user_id TEXT,
    content TEXT NOT NULL,
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_memories_channel_created
ON memories(channel_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_memories_user_created
ON memories(user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS user_notes (
    user_id TEXT PRIMARY KEY,
    display_name TEXT,
    notes TEXT NOT NULL DEFAULT '',
    updated_at REAL NOT NULL
);
"""


class Database:
    def __init__(self, path: str):
        self.path = path
        self.conn: Optional[aiosqlite.Connection] = None
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.executescript(CREATE_TABLES_SQL)
        await self.conn.commit()

    async def close(self) -> None:
        if self.conn is not None:
            await self.conn.close()

    # ── messages ──

    async def log_message(
        self,
        *,
        channel_id: int,
        message_id: int,
        author_id: Optional[int],
        author_name: str,
        content: str,
        is_bot: bool,
    ) -> None:
        assert self.conn is not None
        async with self._lock:
            await self.conn.execute(
                "INSERT INTO messages (channel_id, message_id, author_id, author_name, content, is_bot, ts) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    str(channel_id),
                    str(message_id),
                    str(author_id) if author_id else None,
                    author_name,
                    content[:2000],
                    int(is_bot),
                    time.time(),
                ),
            )
            await self.conn.commit()

    async def get_recent_messages(self, channel_id: int, limit: int) -> list[dict[str, Any]]:
        assert self.conn is not None
        async with self.conn.execute(
            "SELECT * FROM messages WHERE channel_id = ? ORDER BY ts DESC LIMIT ?",
            (str(channel_id), limit),
        ) as cursor:
            rows = await cursor.fetchall()
        rows = list(rows)
        rows.reverse()
        return [dict(r) for r in rows]

    async def prune_old_messages(self, channel_id: int, keep: int) -> None:
        assert self.conn is not None
        async with self._lock:
            await self.conn.execute(
                """
                DELETE FROM messages WHERE id IN (
                    SELECT id FROM messages WHERE channel_id = ?
                    ORDER BY ts DESC LIMIT -1 OFFSET ?
                )
                """,
                (str(channel_id), keep),
            )
            await self.conn.commit()

    # ── memories ──

    async def add_memory(
        self,
        *,
        channel_id: int,
        user_id: Optional[int],
        content: str,
    ) -> None:
        if not content.strip():
            return
        assert self.conn is not None
        async with self._lock:
            await self.conn.execute(
                "INSERT INTO memories (channel_id, user_id, content, created_at) VALUES (?, ?, ?, ?)",
                (
                    str(channel_id),
                    str(user_id) if user_id else None,
                    content[:1200],
                    time.time(),
                ),
            )
            await self.conn.commit()

    async def get_memories(
        self,
        *,
        channel_id: int,
        user_id: Optional[int] = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        assert self.conn is not None
        # Recent channel memories
        async with self.conn.execute(
            "SELECT * FROM memories WHERE channel_id = ? ORDER BY created_at DESC LIMIT ?",
            (str(channel_id), limit),
        ) as cursor:
            channel_rows = [dict(r) for r in await cursor.fetchall()]

        if user_id is None:
            return channel_rows

        # Supplement with user-relevant memories from other channels
        async with self.conn.execute(
            "SELECT * FROM memories WHERE user_id = ? AND channel_id != ? ORDER BY created_at DESC LIMIT ?",
            (str(user_id), str(channel_id), max(0, limit - len(channel_rows))),
        ) as cursor:
            user_rows = [dict(r) for r in await cursor.fetchall()]

        seen = set()
        result = []
        for r in channel_rows + user_rows:
            if r["id"] not in seen:
                seen.add(r["id"])
                result.append(r)
        return result

    # ── user notes ──

    async def upsert_user_note(
        self,
        *,
        user_id: int,
        display_name: str,
        note: str,
    ) -> None:
        if not note.strip():
            return
        user_id_s = str(user_id)
        now = time.time()
        assert self.conn is not None

        async with self._lock:
            existing = await self._fetchone(
                "SELECT notes FROM user_notes WHERE user_id = ?", (user_id_s,)
            )
            if existing is None:
                await self.conn.execute(
                    "INSERT INTO user_notes (user_id, display_name, notes, updated_at) VALUES (?, ?, ?, ?)",
                    (user_id_s, display_name[:100], note[:1000], now),
                )
            else:
                old = existing["notes"] or ""
                if note.strip() not in old:
                    merged = (old + "\n- " + note.strip())[:2500]
                else:
                    merged = old
                await self.conn.execute(
                    "UPDATE user_notes SET display_name=?, notes=?, updated_at=? WHERE user_id=?",
                    (display_name[:100], merged, now, user_id_s),
                )
            await self.conn.commit()

    async def get_user_note(self, user_id: int) -> Optional[dict[str, Any]]:
        row = await self._fetchone(
            "SELECT * FROM user_notes WHERE user_id = ?", (str(user_id),)
        )
        return dict(row) if row else None

    async def _fetchone(
        self, sql: str, params: tuple[Any, ...] = ()
    ) -> Optional[aiosqlite.Row]:
        assert self.conn is not None
        async with self.conn.execute(sql, params) as cursor:
            return await cursor.fetchone()


# ============================================================
# HELPERS
# ============================================================

MASS_MENTION_RE = re.compile(r"@(everyone|here)", re.I)


def sanitize_output(text: str) -> str:
    text = text.strip()
    text = MASS_MENTION_RE.sub(lambda m: m.group(1), text)
    text = text.replace("<@&", "<role:")
    text = text.replace("\x00", "")
    return text


def split_message(text: str, limit: int = 1800) -> list[str]:
    text = text.strip()
    if len(text) <= limit:
        return [text] if text else []

    chunks: list[str] = []
    current: list[str] = []
    current_len = 0

    for paragraph in text.split("\n"):
        if len(paragraph) > limit:
            words = paragraph.split(" ")
            sub: list[str] = []
            sub_len = 0
            for word in words:
                add_len = len(word) + (1 if sub else 0)
                if sub_len + add_len > limit:
                    chunks.append(" ".join(sub))
                    sub = [word]
                    sub_len = len(word)
                else:
                    sub.append(word)
                    sub_len += add_len
            if sub:
                chunks.append(" ".join(sub))
            continue

        add_len = len(paragraph) + (1 if current else 0)
        if current_len + add_len > limit:
            chunks.append("\n".join(current))
            current = [paragraph]
            current_len = len(paragraph)
        else:
            current.append(paragraph)
            current_len += add_len

    if current:
        chunks.append("\n".join(current))

    return [c for c in chunks if c.strip()]


def safe_display_name(user: discord.abc.User) -> str:
    name = getattr(user, "display_name", None) or getattr(user, "name", "unknown")
    return str(name)


def message_to_context_line(msg: discord.Message, bot_user_id: Optional[int]) -> str:
    author = safe_display_name(msg.author)
    if msg.author.id == bot_user_id:
        author = f"{author} (you)"

    parts: list[str] = []
    if msg.content and msg.content.strip():
        parts.append(msg.content.strip())

    if msg.attachments:
        parts.append("[attachments: " + ", ".join(a.filename for a in msg.attachments[:3]) + "]")

    if msg.stickers:
        parts.append("[stickers: " + ", ".join(s.name for s in msg.stickers[:3]) + "]")

    content = " ".join(parts).strip() or "[no text]"
    return f"{author}: {content}"


# ============================================================
# PROMPTS
# ============================================================

PROMPTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prompts")


def _load_template(name: str) -> string.Template:
    path = os.path.join(PROMPTS_DIR, name)
    with open(path, encoding="utf-8") as f:
        return string.Template(f.read())


PROMPTS: dict[str, string.Template] = {}


def _init_prompts() -> None:
    required = ("personality.md", "system.md", "context.md")
    for name in required:
        try:
            PROMPTS[name] = _load_template(name)
        except FileNotFoundError:
            raise SystemExit(
                f"Missing prompt file: {os.path.join(PROMPTS_DIR, name)}\n"
                f"Make sure the prompts/ directory is next to bot.py."
            )


_init_prompts()


# ============================================================
# LLM
# ============================================================

@dataclass
class BotDecision:
    action: Literal["respond", "react", "observe"]
    reply_text: str = ""
    reaction: str = ""
    memories: list[str] = field(default_factory=list)
    user_notes: list[dict[str, str]] = field(default_factory=list)

    @staticmethod
    def from_json(data: dict[str, Any]) -> "BotDecision":
        action = str(data.get("action", "observe")).strip().lower()
        if action not in ("respond", "react", "observe"):
            action = "observe"
        return BotDecision(
            action=action,  # type: ignore[arg-type]
            reply_text=str(data.get("reply_text", "")).strip()[:350],
            reaction=str(data.get("reaction", "")).strip()[:32],
            memories=[
                str(m).strip()[:500]
                for m in data.get("memories", [])
                if str(m).strip()
            ][:5],
            user_notes=[
                {"user_id": str(n.get("user_id", "")), "note": str(n.get("note", ""))[:200]}
                for n in data.get("user_notes", [])
                if n.get("user_id") and n.get("note")
            ][:3],
        )


class LLM:
    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: Optional[str],
        provider: str,
        config: Config,
    ):
        client_kwargs: dict[str, Any] = {"api_key": api_key}
        if base_url:
            client_kwargs["base_url"] = base_url
        if provider == "openrouter":
            client_kwargs["default_headers"] = {
                "HTTP-Referer": "https://github.com/dbot",
                "X-Title": "dbot",
            }
        self.client = AsyncOpenAI(**client_kwargs)
        self.model = model
        self.config = config

    @classmethod
    def _from_config(cls, config: Config) -> "LLM":
        p = config.llm_provider
        if p == "openrouter":
            return cls(
                api_key=config.openrouter_api_key,
                model=config.openrouter_model,
                base_url=OPENROUTER_BASE_URL,
                provider=p,
                config=config,
            )
        elif p == "kilo":
            return cls(
                api_key=config.kilo_api_key,
                model=config.kilo_model,
                base_url=KILO_BASE_URL,
                provider=p,
                config=config,
            )
        return cls(
            api_key=config.openai_api_key,
            model=config.openai_model,
            base_url=None,
            provider=p,
            config=config,
        )

    async def decide(
        self,
        *,
        tagged: bool,
        recent_chat: list[str],
        memories: list[str],
        user_notes: list[str],
    ) -> BotDecision:
        system_prompt = PROMPTS["system.md"].substitute(
            bot_name=self.config.bot_name,
        )

        user_prompt = PROMPTS["context.md"].substitute(
            tagged=str(tagged),
            recent_chat="\n".join(recent_chat) or "[no recent chat]",
            memories="\n".join(memories) or "- none",
            user_notes="\n".join(user_notes) or "- none",
        )

        try:
            response = await self.client.chat.completions.create(
                model=self.model,
                temperature=0.85,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
            )
            content = response.choices[0].message.content or "{}"
            data = json.loads(content)
            return BotDecision.from_json(data)
        except Exception:
            logger.exception("LLM call failed")
            return BotDecision(action="observe")


# ============================================================
# DISCORD BOT
# ============================================================


class SocialDiscordBot(discord.Client):
    def __init__(self, config: Config):
        intents = discord.Intents.default()
        intents.guilds = True
        intents.messages = True
        intents.message_content = True

        super().__init__(intents=intents)

        self.config = config
        self.db = Database(config.database_path)
        self.llm = LLM._from_config(config)
        self.allowed_mentions = discord.AllowedMentions.none()

    async def setup_hook(self) -> None:
        await self.db.connect()
        logger.info("Database connected")

    async def close(self) -> None:
        logger.info("Shutting down…")
        await self.db.close()
        await super().close()
        logger.info("Shutdown complete")

    async def on_ready(self) -> None:
        logger.info(
            "Logged in as %s (%s) | provider=%s | model=%s",
            self.user,
            self.user.id if self.user else "unknown",
            self.config.llm_provider,
            self.llm.model,
        )

    async def on_message(self, message: discord.Message) -> None:
        if self.user is None:
            return
        if message.guild is None:
            return
        if message.author.id == self.user.id:
            return
        if message.author.bot or message.webhook_id is not None:
            return
        if message.type not in {discord.MessageType.default, discord.MessageType.reply}:
            return
        if not isinstance(message.channel, (discord.TextChannel, discord.Thread)):
            return

        content = (message.content or "").strip()
        if not content and not message.attachments and not message.stickers:
            return

        for prefix in self.config.ignore_prefixes:
            if content.startswith(prefix):
                return

        try:
            await self.handle_message(message)
        except Exception:
            logger.exception("Failed handling message %s", message.id)

    async def handle_message(self, message: discord.Message) -> None:
        # Log this message
        await self.db.log_message(
            channel_id=message.channel.id,
            message_id=message.id,
            author_id=message.author.id,
            author_name=safe_display_name(message.author),
            content=message.content or "",
            is_bot=False,
        )

        # Periodically prune old messages (keep 5x history limit)
        await self.db.prune_old_messages(message.channel.id, self.config.history_limit * 5)

        tagged = self.user in message.mentions if self.user else False

        # Always consult the LLM — it decides whether to act
        recent_msgs = await self.db.get_recent_messages(
            message.channel.id, self.config.history_limit
        )
        recent_lines = [
            f"{m['author_name']}: {m['content']}" for m in recent_msgs
        ]

        memories = await self.db.get_memories(
            channel_id=message.channel.id,
            user_id=message.author.id,
            limit=10,
        )
        memory_texts = [m["content"] for m in memories]

        user_note = await self.db.get_user_note(message.author.id)
        user_notes = []
        if user_note and user_note.get("notes"):
            name = user_note.get("display_name") or safe_display_name(message.author)
            user_notes.append(f"{name}: {user_note['notes']}")

        decision = await self.llm.decide(
            tagged=tagged,
            recent_chat=recent_lines,
            memories=memory_texts,
            user_notes=user_notes,
        )

        # Store any memories the LLM wanted to save
        for mem in decision.memories:
            await self.db.add_memory(
                channel_id=message.channel.id,
                user_id=message.author.id,
                content=mem,
            )

        # Store any user notes
        for note in decision.user_notes:
            try:
                uid = int(note["user_id"])
                await self.db.upsert_user_note(
                    user_id=uid,
                    display_name=safe_display_name(message.author),
                    note=note["note"],
                )
            except (ValueError, KeyError):
                pass

        # Execute action
        if decision.action == "react" and decision.reaction:
            try:
                await message.add_reaction(decision.reaction)
                logger.info("REACT | channel=%s | emoji=%s", message.channel.id, decision.reaction)
            except Exception as e:
                logger.warning("Reaction failed: %s", e)

        elif decision.action == "respond" and decision.reply_text.strip():
            text = sanitize_output(decision.reply_text)
            if text:
                await self.send_reply(message=message, text=text)
                await self.db.log_message(
                    channel_id=message.channel.id,
                    message_id=0,
                    author_id=self.user.id if self.user else None,
                    author_name=self.config.bot_name,
                    content=text[:500],
                    is_bot=True,
                )

    async def send_reply(self, *, message: discord.Message, text: str) -> None:
        chunks = split_message(text)
        if not chunks:
            return

        delay = max(
            self.config.reply_min_delay_seconds,
            min(
                len(text) / max(1.0, self.config.typing_chars_per_second),
                self.config.reply_max_delay_seconds,
            ),
        )

        async with message.channel.typing():
            await asyncio.sleep(delay + random.uniform(0.1, 0.8))

        for idx, chunk in enumerate(chunks):
            if idx == 0:
                await message.reply(
                    chunk,
                    mention_author=False,
                    allowed_mentions=self.allowed_mentions,
                )
            else:
                await message.channel.send(
                    chunk,
                    allowed_mentions=self.allowed_mentions,
                )

        logger.info("REPLY | channel=%s | len=%d", message.channel.id, len(text))


# ============================================================
# SETUP & CLI
# ============================================================

PROVIDER_DEFAULTS: dict[str, dict[str, str]] = {
    "openai":     {"model": "gpt-4o-mini",                       "env": "OPENAI_API_KEY"},
    "openrouter": {"model": "arcee-ai/trinity-large-preview:free", "env": "OPENROUTER_API_KEY"},
    "kilo":       {"model": "stepfun/step-3.5-flash:free",       "env": "KILO_API_KEY"},
}

ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")


def interactive_setup() -> dict[str, str]:
    print("\n  dbot setup\n")

    token = input("  Discord bot token: ").strip()
    if not token:
        raise SystemExit("  error: token is required")

    print("\n  LLM provider:")
    print("    1) openrouter  (arcee-ai/trinity-large-preview:free – free)")
    print("    2) kilo        (stepfun/step-3.5-flash:free – free)")
    print("    3) openai      (gpt-4o-mini – paid)")
    choice = input("\n  Pick [1]: ").strip() or "1"
    provider_map = {"1": "openrouter", "2": "kilo", "3": "openai"}
    provider = provider_map.get(choice, "openrouter")

    pd = PROVIDER_DEFAULTS[provider]
    api_key = input(f"\n  {pd['env']}: ").strip()
    if not api_key:
        raise SystemExit("  error: API key is required")

    model = input(f"\n  Model [{pd['model']}]: ").strip() or pd["model"]

    lines = [
        f"DISCORD_TOKEN={token}",
        f"LLM_PROVIDER={provider}",
        "",
    ]
    for p, info in PROVIDER_DEFAULTS.items():
        key_name = info["env"]
        if p == provider:
            lines.append(f"{key_name}={api_key}")
        else:
            lines.append(f"{key_name}=")
    lines.append("")
    for p, info in PROVIDER_DEFAULTS.items():
        model_name = {"openai": "OPENAI_MODEL", "openrouter": "OPENROUTER_MODEL", "kilo": "KILO_MODEL"}[p]
        val = model if p == provider else ""
        lines.append(f"{model_name}={val}")

    with open(ENV_PATH, "w") as f:
        f.write("\n".join(lines) + "\n")

    print(f"\n  saved to {ENV_PATH}")
    print(f"  provider={provider}  model={model}\n")

    return {
        "DISCORD_TOKEN": token,
        "LLM_PROVIDER": provider,
        PROVIDER_DEFAULTS[provider]["env"]: api_key,
        {"openai": "OPENAI_MODEL", "openrouter": "OPENROUTER_MODEL", "kilo": "KILO_MODEL"}[provider]: model,
    }


def parse_args() -> tuple[dict[str, str], bool]:
    p = argparse.ArgumentParser(
        description="dbot – a Discord bot with LLM-powered cognition",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
examples:
  python bot.py --setup
  python bot.py --token TOKEN --provider openrouter --api-key KEY
  python bot.py
""",
    )
    p.add_argument("--setup",      action="store_true", help="Run interactive setup wizard")
    p.add_argument("--token",      help="Discord bot token")
    p.add_argument("--provider",   help="LLM provider: openai | openrouter | kilo",
                   choices=["openai", "openrouter", "kilo"])
    p.add_argument("--api-key",    help="API key for the chosen provider")
    p.add_argument("--model",      help="Model override (uses provider default if omitted)")
    args = p.parse_args()
    overrides = {k: v for k, v in vars(args).items() if v not in (None, False)}
    return overrides, args.setup


def main() -> None:
    if sys.version_info < (3, 10):
        raise SystemExit("dbot requires Python 3.10 or later")

    overrides, wants_setup = parse_args()

    if wants_setup:
        overrides = interactive_setup()

    if "token" in overrides:
        overrides["DISCORD_TOKEN"] = overrides.pop("token")
    if "provider" in overrides:
        overrides["LLM_PROVIDER"] = overrides.pop("provider")
    if "model" in overrides:
        provider = overrides.get("LLM_PROVIDER", os.getenv("LLM_PROVIDER", "openai"))
        model_key = {"openai": "OPENAI_MODEL", "openrouter": "OPENROUTER_MODEL", "kilo": "KILO_MODEL"}
        overrides[model_key.get(provider, "OPENAI_MODEL")] = overrides.pop("model")

    config = Config.load(overrides)

    logging.basicConfig(
        level=getattr(logging, config.log_level, logging.INFO),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        force=True,
    )

    if not config.discord_token:
        print("\n  No config found. Run with --setup to get started:\n")
        print("    python bot.py --setup\n")
        raise SystemExit(1)

    provider = config.llm_provider
    pd = PROVIDER_DEFAULTS[provider]
    api_key = getattr(config, pd["env"].lower())
    if not api_key:
        raise RuntimeError(
            f"Missing API key for provider '{provider}'. "
            f"Pass --api-key YOUR_KEY or set {pd['env']} in .env"
        )

    client = SocialDiscordBot(config)
    client.run(config.discord_token, log_handler=None)


if __name__ == "__main__":
    main()
