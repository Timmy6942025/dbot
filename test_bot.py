"""Tests for dbot."""

from __future__ import annotations

import os
import json
import time
import asyncio
from typing import Any
from unittest.mock import patch

import pytest
import pytest_asyncio

# Ensure .env doesn't interfere with Config tests
os.environ.setdefault("DISCORD_TOKEN", "")
os.environ.setdefault("OPENAI_API_KEY", "")

from bot import (
    Config,
    Database,
    BotDecision,
    LLM,
    PROMPTS,
    sanitize_output,
    split_message,
)


# ============================================================
# HELPERS
# ============================================================


class TestSanitizeOutput:
    def test_strips_whitespace(self):
        assert sanitize_output("  hello  ") == "hello"

    def test_strips_everyone_mention(self):
        assert sanitize_output("@everyone hello") == "everyone hello"

    def test_strips_here_mention(self):
        assert sanitize_output("hey @here") == "hey here"

    def test_case_insensitive(self):
        assert sanitize_output("@Everyone") == "Everyone"

    def test_strips_role_mentions(self):
        assert sanitize_output("<@&12345>") == "<role:12345>"

    def test_strips_null_bytes(self):
        assert sanitize_output("hello\x00world") == "helloworld"

    def test_combined(self):
        result = sanitize_output("  @everyone look at <@&999>\x00  ")
        assert result == "everyone look at <role:999>"

    def test_empty_string(self):
        assert sanitize_output("") == ""

    def test_normal_text(self):
        assert sanitize_output("hello world") == "hello world"


class TestSplitMessage:
    def test_short_message(self):
        assert split_message("hello") == ["hello"]

    def test_empty_string(self):
        assert split_message("") == []

    def test_whitespace_only(self):
        assert split_message("   ") == []

    def test_exact_limit(self):
        text = "a" * 1800
        assert split_message(text) == [text]

    def test_just_over_limit(self):
        text = "a" * 1801
        result = split_message(text)
        assert len(result) >= 1
        assert sum(len(c) for c in result) >= 1801

    def test_split_on_paragraph(self):
        para1 = "a" * 900
        para2 = "b" * 900
        result = split_message(f"{para1}\n{para2}")
        assert len(result) == 2
        assert para1 in result[0]
        assert para2 in result[1]

    def test_split_long_word(self):
        text = "word " + "x" * 1801
        result = split_message(text)
        assert len(result) >= 2

    def test_multi_paragraph(self):
        paras = [f"paragraph {i}" for i in range(50)]
        text = "\n".join(paras)
        result = split_message(text)
        rejoined = "\n".join(result)
        assert rejoined == text

    def test_no_empty_chunks(self):
        text = "\n\n\n" + "a" * 3000 + "\n\n\n"
        result = split_message(text)
        for chunk in result:
            assert chunk.strip()


# ============================================================
# CONFIG
# ============================================================


class TestConfig:
    def test_load_with_overrides(self):
        config = Config.load({
            "DISCORD_TOKEN": "test-token",
            "LLM_PROVIDER": "openrouter",
            "OPENROUTER_API_KEY": "test-key",
        })
        assert config.discord_token == "test-token"
        assert config.llm_provider == "openrouter"
        assert config.openrouter_api_key == "test-key"

    def test_defaults(self):
        config = Config.load({"DISCORD_TOKEN": "t", "OPENAI_API_KEY": "k"})
        assert config.history_limit == 20
        assert config.ignore_prefixes == ("!", "/", ".")
        assert config.reply_min_delay_seconds == 1.0
        assert config.reply_max_delay_seconds == 8.0
        assert config.openai_model == "gpt-4o-mini"
        assert config.openrouter_model == "arcee-ai/trinity-large-preview:free"
        assert config.kilo_model == "stepfun/step-3.5-flash:free"

    def test_invalid_provider_raises(self):
        with pytest.raises(ValueError, match="Invalid LLM_PROVIDER"):
            Config.load({"LLM_PROVIDER": "taco"})

    def test_api_key_routing(self):
        config = Config.load({
            "DISCORD_TOKEN": "t",
            "LLM_PROVIDER": "openrouter",
            "api_key": "routed-key",
        })
        assert config.openrouter_api_key == "routed-key"

    def test_api_key_routing_kilo(self):
        config = Config.load({
            "DISCORD_TOKEN": "t",
            "LLM_PROVIDER": "kilo",
            "api_key": "kilo-key",
        })
        assert config.kilo_api_key == "kilo-key"

    def test_override_takes_priority(self):
        with patch.dict(os.environ, {"HISTORY_LIMIT": "99"}):
            config = Config.load({"HISTORY_LIMIT": "5"})
            assert config.history_limit == 5

    def test_env_fallback(self):
        with patch.dict(os.environ, {"HISTORY_LIMIT": "42"}):
            config = Config.load({})
            assert config.history_limit == 42

    def test_frozen(self):
        config = Config.load({"DISCORD_TOKEN": "t", "OPENAI_API_KEY": "k"})
        with pytest.raises(AttributeError):
            config.discord_token = "other"


# ============================================================
# DATABASE
# ============================================================


@pytest_asyncio.fixture
async def db():
    d = Database(":memory:")
    await d.connect()
    yield d
    await d.close()


class TestDatabaseMessages:
    @pytest.mark.asyncio
    async def test_log_and_retrieve(self, db: Database):
        await db.log_message(
            channel_id=100, message_id=1, author_id=42,
            author_name="Alice", content="hello", is_bot=False,
        )
        msgs = await db.get_recent_messages(100, 10)
        assert len(msgs) == 1
        assert msgs[0]["content"] == "hello"
        assert msgs[0]["author_name"] == "Alice"
        assert msgs[0]["is_bot"] == 0

    @pytest.mark.asyncio
    async def test_order_is_chronological(self, db: Database):
        for i in range(5):
            await db.log_message(
                channel_id=100, message_id=i, author_id=1,
                author_name="A", content=f"msg{i}", is_bot=False,
            )
        msgs = await db.get_recent_messages(100, 10)
        assert [m["content"] for m in msgs] == [f"msg{i}" for i in range(5)]

    @pytest.mark.asyncio
    async def test_limit(self, db: Database):
        for i in range(10):
            await db.log_message(
                channel_id=100, message_id=i, author_id=1,
                author_name="A", content=f"msg{i}", is_bot=False,
            )
        msgs = await db.get_recent_messages(100, 3)
        assert len(msgs) == 3
        assert msgs[0]["content"] == "msg7"

    @pytest.mark.asyncio
    async def test_prune_keeps_recent(self, db: Database):
        for i in range(10):
            await db.log_message(
                channel_id=100, message_id=i, author_id=1,
                author_name="A", content=f"msg{i}", is_bot=False,
            )
        await db.prune_old_messages(100, 3)
        msgs = await db.get_recent_messages(100, 100)
        assert len(msgs) == 3
        assert msgs[0]["content"] == "msg7"

    @pytest.mark.asyncio
    async def test_prune_isolation(self, db: Database):
        await db.log_message(channel_id=1, message_id=1, author_id=1, author_name="A", content="ch1", is_bot=False)
        await db.log_message(channel_id=2, message_id=2, author_id=1, author_name="A", content="ch2", is_bot=False)
        await db.prune_old_messages(1, 0)
        assert len(await db.get_recent_messages(1, 10)) == 0
        assert len(await db.get_recent_messages(2, 10)) == 1


class TestDatabaseMemories:
    @pytest.mark.asyncio
    async def test_add_and_retrieve(self, db: Database):
        await db.add_memory(channel_id=100, user_id=42, content="Alice likes cats")
        mems = await db.get_memories(channel_id=100)
        assert len(mems) == 1
        assert mems[0]["content"] == "Alice likes cats"

    @pytest.mark.asyncio
    async def test_empty_content_ignored(self, db: Database):
        await db.add_memory(channel_id=100, user_id=42, content="")
        await db.add_memory(channel_id=100, user_id=42, content="   ")
        mems = await db.get_memories(channel_id=100)
        assert len(mems) == 0

    @pytest.mark.asyncio
    async def test_cross_channel_user_memories(self, db: Database):
        await db.add_memory(channel_id=1, user_id=42, content="from ch1")
        await db.add_memory(channel_id=2, user_id=42, content="from ch2")
        mems = await db.get_memories(channel_id=2, user_id=42, limit=10)
        contents = [m["content"] for m in mems]
        assert "from ch2" in contents
        assert "from ch1" in contents

    @pytest.mark.asyncio
    async def test_no_duplicate_ids(self, db: Database):
        await db.add_memory(channel_id=1, user_id=42, content="mem")
        mems = await db.get_memories(channel_id=1, user_id=42, limit=10)
        ids = [m["id"] for m in mems]
        assert len(ids) == len(set(ids))

    @pytest.mark.asyncio
    async def test_content_truncated(self, db: Database):
        long_content = "x" * 2000
        await db.add_memory(channel_id=1, user_id=1, content=long_content)
        mems = await db.get_memories(channel_id=1)
        assert len(mems[0]["content"]) <= 1200

    @pytest.mark.asyncio
    async def test_no_user_id(self, db: Database):
        await db.add_memory(channel_id=100, user_id=None, content="general memory")
        mems = await db.get_memories(channel_id=100)
        assert len(mems) == 1
        assert mems[0]["user_id"] is None


class TestDatabaseUserNotes:
    @pytest.mark.asyncio
    async def test_insert_new(self, db: Database):
        await db.upsert_user_note(user_id=42, display_name="Alice", note="likes cats")
        note = await db.get_user_note(42)
        assert note is not None
        assert note["notes"] == "likes cats"
        assert note["display_name"] == "Alice"

    @pytest.mark.asyncio
    async def test_update_existing(self, db: Database):
        await db.upsert_user_note(user_id=42, display_name="Alice", note="likes cats")
        await db.upsert_user_note(user_id=42, display_name="Alice", note="likes dogs")
        note = await db.get_user_note(42)
        assert "likes cats" in note["notes"]
        assert "likes dogs" in note["notes"]

    @pytest.mark.asyncio
    async def test_no_duplicate_notes(self, db: Database):
        await db.upsert_user_note(user_id=42, display_name="Alice", note="likes cats")
        await db.upsert_user_note(user_id=42, display_name="Alice", note="likes cats")
        note = await db.get_user_note(42)
        assert note["notes"].count("likes cats") == 1

    @pytest.mark.asyncio
    async def test_empty_note_ignored(self, db: Database):
        await db.upsert_user_note(user_id=42, display_name="Alice", note="")
        await db.upsert_user_note(user_id=42, display_name="Alice", note="   ")
        note = await db.get_user_note(42)
        assert note is None

    @pytest.mark.asyncio
    async def test_nonexistent_user_returns_none(self, db: Database):
        note = await db.get_user_note(999)
        assert note is None

    @pytest.mark.asyncio
    async def test_display_name_updated(self, db: Database):
        await db.upsert_user_note(user_id=42, display_name="Alice", note="note1")
        await db.upsert_user_note(user_id=42, display_name="Alice_", note="note2")
        note = await db.get_user_note(42)
        assert note["display_name"] == "Alice_"


# ============================================================
# BOT DECISION
# ============================================================


class TestBotDecision:
    def test_parse_respond(self):
        d = BotDecision.from_json({
            "action": "respond",
            "reply_text": "hello!",
            "reaction": "",
            "memories": ["Alice said hi"],
            "user_notes": [],
        })
        assert d.action == "respond"
        assert d.reply_text == "hello!"
        assert d.memories == ["Alice said hi"]

    def test_parse_observe(self):
        d = BotDecision.from_json({"action": "observe"})
        assert d.action == "observe"
        assert d.reply_text == ""
        assert d.reaction == ""
        assert d.memories == []
        assert d.user_notes == []

    def test_parse_react(self):
        d = BotDecision.from_json({"action": "react", "reaction": "👍"})
        assert d.action == "react"
        assert d.reaction == "👍"
        assert d.reply_text == ""

    def test_invalid_action_defaults_to_observe(self):
        d = BotDecision.from_json({"action": "dance"})
        assert d.action == "observe"

    def test_missing_action_defaults_to_observe(self):
        d = BotDecision.from_json({})
        assert d.action == "observe"

    def test_reply_text_truncated(self):
        d = BotDecision.from_json({"action": "respond", "reply_text": "x" * 500})
        assert len(d.reply_text) <= 350

    def test_memories_capped(self):
        d = BotDecision.from_json({
            "action": "respond",
            "memories": [f"mem{i}" for i in range(20)],
        })
        assert len(d.memories) <= 5

    def test_user_notes_parsed(self):
        d = BotDecision.from_json({
            "action": "respond",
            "user_notes": [
                {"user_id": "42", "note": "likes cats"},
                {"user_id": "", "note": "no id"},
                {"user_id": "99", "note": ""},
            ],
        })
        assert len(d.user_notes) == 1
        assert d.user_notes[0]["user_id"] == "42"

    def test_empty_memories_filtered(self):
        d = BotDecision.from_json({
            "action": "respond",
            "memories": ["", "  ", "valid"],
        })
        assert d.memories == ["valid"]


# ============================================================
# LLM
# ============================================================


class TestLLM:
    def test_prompt_rendering(self):
        personality = PROMPTS["personality.md"].substitute(bot_name="dbot").strip()
        system = PROMPTS["system.md"].substitute(personality=personality)
        assert "dbot" in system
        assert "respond" in system
        assert "observe" in system

    def test_context_rendering(self):
        context = PROMPTS["context.md"].substitute(
            tagged="True",
            recent_chat="Alice: hello\nBob: hi",
            memories="- Alice likes cats",
            user_notes="Alice: likes technology",
        )
        assert "True" in context
        assert "Alice: hello" in context
        assert "Alice likes cats" in context

    def test_from_config_openai(self):
        config = Config.load({
            "DISCORD_TOKEN": "t",
            "LLM_PROVIDER": "openai",
            "OPENAI_API_KEY": "sk-test",
            "OPENAI_MODEL": "gpt-4o",
        })
        llm = LLM._from_config(config)
        assert llm.model == "gpt-4o"

    def test_from_config_openrouter(self):
        config = Config.load({
            "DISCORD_TOKEN": "t",
            "LLM_PROVIDER": "openrouter",
            "OPENROUTER_API_KEY": "sk-or-test",
        })
        llm = LLM._from_config(config)
        assert llm.model == "arcee-ai/trinity-large-preview:free"

    def test_from_config_kilo(self):
        config = Config.load({
            "DISCORD_TOKEN": "t",
            "LLM_PROVIDER": "kilo",
            "KILO_API_KEY": "ki-test",
        })
        llm = LLM._from_config(config)
        assert llm.model == "stepfun/step-3.5-flash:free"
