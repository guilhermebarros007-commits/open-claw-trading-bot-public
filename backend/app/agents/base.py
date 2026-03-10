import logging
import os
from typing import AsyncIterator
import google.generativeai as genai
from pathlib import Path
from datetime import datetime
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from app.memory import db as memory_db

logger = logging.getLogger(__name__)

WORKSPACE_ROOT = Path(__file__).parent.parent.parent / "workspaces"
MEMORY_MAX_CHARS = 4000

# ── Per-agent model selection ─────────────────────────────────────────────────
# Lux (orchestrator) → Gemini 2.0 Flash: strong reasoning
# Traders → Gemini 2.0 Flash Lite: fast and efficient
AGENT_MODELS: dict[str, str] = {
    "lux":        os.getenv("GEMINI_MODEL_LUX", "gemini-2.0-flash"),
    "hype_beast": os.getenv("GEMINI_MODEL_TRADERS", "gemini-2.0-flash-lite"),
    "oracle":     os.getenv("GEMINI_MODEL_TRADERS", "gemini-2.0-flash-lite"),
    "vitalik":    os.getenv("GEMINI_MODEL_TRADERS", "gemini-2.0-flash-lite"),
}

AGENT_MAX_TOKENS: dict[str, int] = {
    "lux":        1024,
    "hype_beast": 512,
    "oracle":     512,
    "vitalik":    512,
}

_DEFAULT_MODEL = os.getenv("GEMINI_MODEL_LUX", "gemini-2.0-flash")


class BaseAgent:
    def __init__(self, agent_id: str):
        self.agent_id = agent_id
        self.workspace = WORKSPACE_ROOT / agent_id
        # Client will be initialized in specific provider classes
        self._model = AGENT_MODELS.get(agent_id, _DEFAULT_MODEL)
        self._max_tokens = AGENT_MAX_TOKENS.get(agent_id, 1024)
        logger.info(f"[{agent_id}] model={self._model} max_tokens={self._max_tokens}")

    def _read_file(self, path: Path) -> str:
        if path.exists():
            return path.read_text(encoding="utf-8")
        return ""

    def _build_system_blocks(self) -> list[dict]:
        """
        Returns system prompt as content blocks with prompt caching.

        Block 1 (cached): SOUL.md + all skills/*.md  — static, never changes
        Block 2 (dynamic): MEMORY.md               — changes after each heartbeat,
                                                      NOT cached to avoid stale hits

        Cache minimum: 1024 tokens (Sonnet) / 2048 tokens (Haiku).
        Caching still helps for chat sessions where the user sends multiple
        messages without a heartbeat in between.
        """
        # ── Static block (cache_control: ephemeral) ───────────────────────────
        static_parts = []
        soul = self._read_file(self.workspace / "SOUL.md")
        if soul:
            static_parts.append(soul)

        skills_dir = self.workspace / "skills"
        if skills_dir.exists():
            for skill_file in sorted(skills_dir.glob("*.md")):
                content = self._read_file(skill_file)
                if content:
                    static_parts.append(f"\n---\n# Skill: {skill_file.stem}\n{content}")

        blocks: list[dict] = [
            {
                "type": "text",
                "text": "\n".join(static_parts),
                "cache_control": {"type": "ephemeral"},
            }
        ]

        # ── Dynamic block (no cache) ───────────────────────────────────────────
        memory = self._read_file(self.workspace / "MEMORY.md")
        if memory and len(memory.strip()) > 20:
            if len(memory) > MEMORY_MAX_CHARS:
                memory = memory[-MEMORY_MAX_CHARS:]
            blocks.append(
                {
                    "type": "text",
                    "text": f"\n---\n# Memória Acumulada\n{memory}",
                }
            )

        return blocks

    async def chat(
        self, user_message: str, extra_context: str = "", history_limit: int = 10
    ) -> str:
        system_blocks = self._build_system_blocks()

        past = await memory_db.get_chat_history(self.agent_id, limit=history_limit)
        messages = [{"role": m["role"], "content": m["content"]} for m in past]

        full_message = f"{extra_context}\n\n{user_message}" if extra_context else user_message
        messages.append({"role": "user", "content": full_message})

        response = await self._client.messages.create(
            model=self._model,
            max_tokens=self._max_tokens,
            system=system_blocks,
            messages=messages,
        )
        reply = response.content[0].text

        # Log cache usage when available
        usage = getattr(response, "usage", None)
        if usage:
            cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
            cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
            if cache_read or cache_write:
                logger.debug(
                    f"[{self.agent_id}] cache_read={cache_read} cache_write={cache_write} "
                    f"input={usage.input_tokens} output={usage.output_tokens}"
                )

        await memory_db.save_message(self.agent_id, "user", full_message)
        await memory_db.save_message(self.agent_id, "assistant", reply)
        return reply

    async def stream_chat(
        self, user_message: str, extra_context: str = "", history_limit: int = 10
    ) -> AsyncIterator[str]:
        """Yields text chunks via Anthropic streaming API with prompt caching."""
        system_blocks = self._build_system_blocks()

        past = await memory_db.get_chat_history(self.agent_id, limit=history_limit)
        messages = [{"role": m["role"], "content": m["content"]} for m in past]

        full_message = f"{extra_context}\n\n{user_message}" if extra_context else user_message
        messages.append({"role": "user", "content": full_message})

        full_reply: list[str] = []
        async with self._client.messages.stream(
            model=self._model,
            max_tokens=self._max_tokens,
            system=system_blocks,
            messages=messages,
        ) as stream:
            async for text in stream.text_stream:
                full_reply.append(text)
                yield text

        reply = "".join(full_reply)
        await memory_db.save_message(self.agent_id, "user", full_message)
        await memory_db.save_message(self.agent_id, "assistant", reply)

    async def append_memory(self, entry: str):
        memory_path = self.workspace / "MEMORY.md"
        timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
        block = f"\n## [{timestamp}]\n{entry}\n"

        if memory_path.exists():
            existing = memory_path.read_text(encoding="utf-8")
            lines = existing.split("\n", 4)
            header = "\n".join(lines[:4]) if len(lines) >= 4 else existing
            rest = "\n".join(lines[4:]) if len(lines) >= 4 else ""
            new_content = header + block + rest
        else:
            new_content = f"# MEMORY.md — {self.agent_id}\n\n---\n{block}"

        memory_path.write_text(new_content, encoding="utf-8")


class GeminiBaseAgent(BaseAgent):
    """
    Base agent using Google Gemini 2.0.
    Supports individual API keys per agent.
    """
    def __init__(self, agent_id: str):
        super().__init__(agent_id)
        
        # Determine the specific key for this agent
        key_map = {
            "lux":        "GOOGLE_API_KEY_LUX",
            "hype_beast": "GOOGLE_API_KEY_HYPE",
            "oracle":     "GOOGLE_API_KEY_ORACLE",
            "vitalik":    "GOOGLE_API_KEY_VITALIK"
        }
        env_var = key_map.get(agent_id, "GOOGLE_API_KEY_LUX")
        api_key = os.getenv(env_var)
        
        if not api_key:
            logger.warning(f"[{agent_id}] No specific API key found for {env_var}, falling back to default.")
            api_key = os.getenv("GOOGLE_API_KEY_LUX")

        genai.configure(api_key=api_key)
        self._client = genai.GenerativeModel(model_name=self._model)
        logger.info(f"[{agent_id}] model={self._model} (Gemini) max_tokens={self._max_tokens}")

    def _build_system_string(self) -> str:
        """Builds plain-text system prompt for Gemini."""
        parts = []
        soul = self._read_file(self.workspace / "SOUL.md")
        if soul:
            parts.append(soul)
        skills_dir = self.workspace / "skills"
        if skills_dir.exists():
            for skill_file in sorted(skills_dir.glob("*.md")):
                content = self._read_file(skill_file)
                if content:
                    parts.append(f"\n---\n# Skill: {skill_file.stem}\n{content}")
        memory = self._read_file(self.workspace / "MEMORY.md")
        if memory and len(memory.strip()) > 20:
            if len(memory) > MEMORY_MAX_CHARS:
                memory = memory[-MEMORY_MAX_CHARS:]
            parts.append(f"\n---\n# Memória Acumulada\n{memory}")
        return "\n".join(parts)

    async def chat(
        self, user_message: str, extra_context: str = "", history_limit: int = 10
    ) -> str:
        system_str = self._build_system_string()
        past = await memory_db.get_chat_history(self.agent_id, limit=history_limit)

        history = []
        for m in past:
            role = "user" if m["role"] == "user" else "model"
            history.append({"role": role, "parts": [m["content"]]})

        full_message = f"{extra_context}\n\n{user_message}" if extra_context else user_message
        
        # Re-creating model with system instruction for better compliance
        model_with_sys = genai.GenerativeModel(
            model_name=self._model,
            system_instruction=system_str
        )
        chat_session = model_with_sys.start_chat(history=history)
        
        @retry(
            stop=stop_after_attempt(3),
            wait=wait_exponential(multiplier=1, min=2, max=10),
            reraise=True
        )
        async def _send_with_retry():
            return await chat_session.send_message_async(
                full_message,
                generation_config=genai.types.GenerationConfig(
                    max_output_tokens=self._max_tokens
                )
            )

        response = await _send_with_retry()
        reply = response.text

        await memory_db.save_message(self.agent_id, "user", full_message)
        await memory_db.save_message(self.agent_id, "assistant", reply)
        return reply

    def _extract_json(self, text: str) -> dict:
        """Robustly extracts JSON from potentially noisy LLM output."""
        import re
        import json
        if not text:
            return {}
        try:
            # Try plain parse first
            return json.loads(text)
        except Exception:
            # Try finding the first { and last }
            # Use a more careful match to handle nested structures
            match = re.search(r"(\{.*\})", text, re.DOTALL)
            if match:
                content = match.group(1)
                try:
                    return json.loads(content)
                except Exception:
                    # Clean up common LLM artifacts if simple JSON fails
                    content = re.sub(r"//.*", "", content) # remove comments
                    try:
                        return json.loads(content)
                    except Exception:
                        pass
        logger.warning(f"[{self.agent_id}] Failed to extract JSON from: {text[:100]}...")
        return {}

    async def stream_chat(
        self, user_message: str, extra_context: str = "", history_limit: int = 10
    ) -> AsyncIterator[str]:
        """Streaming via Gemini."""
        system_str = self._build_system_string()
        past = await memory_db.get_chat_history(self.agent_id, limit=history_limit)

        history = []
        for m in past:
            role = "user" if m["role"] == "user" else "model"
            history.append({"role": role, "parts": [m["content"]]})

        full_message = f"{extra_context}\n\n{user_message}" if extra_context else user_message
        
        model_with_sys = genai.GenerativeModel(
            model_name=self._model,
            system_instruction=system_str
        )
        chat_session = model_with_sys.start_chat(history=history)
        
        response = await chat_session.send_message_async(
            full_message,
            generation_config=genai.types.GenerationConfig(
                max_output_tokens=self._max_tokens
            ),
            stream=True
        )
        
        full_reply = []
        async for chunk in response:
            text = chunk.text
            full_reply.append(text)
            yield text

        reply = "".join(full_reply)
        await memory_db.save_message(self.agent_id, "user", full_message)
        await memory_db.save_message(self.agent_id, "assistant", reply)
