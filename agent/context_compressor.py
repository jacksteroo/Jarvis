"""
Context compression for Pepper conversations.

When a long conversation approaches the model's context window limit, this
module compresses the oldest turns into a summary block while keeping the most
recent turns verbatim.  The summary is injected as a system message so the LLM
has continuity without the full history.

Privacy invariant: summarization ALWAYS runs on the local Ollama model.
Raw conversation history must never be sent to Claude/Anthropic for
compression, even under failure or fallback conditions.  This invariant is
enforced in two layers:
  1. _summarize() constructs the model string directly from DEFAULT_LOCAL_MODEL
     — it never reads DEFAULT_FRONTIER_MODEL.
  2. The chat() call passes local_only=True so the ModelClient refuses to
     route to a frontier model even if the model string were wrong.
"""
from __future__ import annotations

import structlog

logger = structlog.get_logger()

# Approximate chars per token for LLaMA-family models (rough but sufficient).
# We err on the side of compressing *earlier* rather than later.
_CHARS_PER_TOKEN: int = 4

# Fraction of the context window that triggers compression.
_COMPRESSION_THRESHOLD: float = 0.80

# How many recent conversation turns (user + assistant pairs) to keep verbatim.
_DEFAULT_ANCHOR_TURNS: int = 6


class ContextCompressor:
    """Compresses conversation history to stay within a model's context window.

    Usage::

        compressor = ContextCompressor(llm_client, memory_manager, config)

        # Before each LLM call:
        if compressor.needs_compression(messages):
            messages = await compressor.compress(messages)
    """

    def __init__(self, llm_client, memory_manager, config) -> None:
        self._llm = llm_client
        self._memory = memory_manager
        self._config = config

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def estimate_tokens(self, messages: list[dict]) -> int:
        """Return a rough token estimate for a list of messages.

        Uses a character-count heuristic (~4 chars/token for LLaMA-family
        models).  Good enough for deciding when to trigger compression.
        """
        total_chars = sum(len(m.get("content") or "") for m in messages)
        return total_chars // _CHARS_PER_TOKEN

    def needs_compression(self, messages: list[dict]) -> bool:
        """Return True when messages exceed the compression threshold."""
        context_window: int = self._config.MODEL_CONTEXT_TOKENS
        estimated = self.estimate_tokens(messages)
        threshold = int(context_window * _COMPRESSION_THRESHOLD)
        if estimated > threshold:
            logger.info(
                "compression_triggered",
                estimated_tokens=estimated,
                threshold=threshold,
                context_window=context_window,
                n_messages=len(messages),
            )
            return True
        return False

    async def compress(
        self,
        messages: list[dict],
        anchor_turns: int = _DEFAULT_ANCHOR_TURNS,
    ) -> list[dict]:
        """Compress older messages, keeping anchor_turns recent turns verbatim.

        Steps:
        1. Separate system messages from conversation messages.
        2. Keep the last anchor_turns * 2 conversation messages (anchor).
        3. Save older messages to recall memory so nothing is truly lost.
        4. Summarize older messages using the LOCAL model only.
        5. Return: original system messages + summary block + anchor messages.

        Privacy invariant: step 4 always uses the local model.
        """
        system_messages = [m for m in messages if m.get("role") == "system"]
        conv_messages = [m for m in messages if m.get("role") != "system"]

        # Each "turn" = one user message + one assistant message
        turns_to_keep = anchor_turns * 2
        if len(conv_messages) <= turns_to_keep:
            logger.debug(
                "compress_skipped_few_turns",
                n_conv=len(conv_messages),
                turns_to_keep=turns_to_keep,
            )
            return messages

        messages_to_compress = conv_messages[:-turns_to_keep]
        anchor_messages = conv_messages[-turns_to_keep:]

        # Save pre-compression turns to recall memory before discarding them.
        await self._save_to_recall(messages_to_compress)

        # Summarize — LOCAL model only (see privacy invariant above).
        summary = await self._summarize(messages_to_compress)

        summary_message = {
            "role": "system",
            "content": f"[Summary of earlier conversation: {summary}]",
        }

        compressed = system_messages + [summary_message] + anchor_messages
        logger.info(
            "context_compressed",
            turns_compressed=len(messages_to_compress) // 2,
            turns_kept=len(anchor_messages) // 2,
            original_messages=len(messages),
            compressed_messages=len(compressed),
            summary_preview=summary[:120],
        )
        return compressed

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _summarize(self, messages: list[dict]) -> str:
        """Summarize older conversation turns into a compact paragraph.

        Privacy invariant enforced here:
        - Model string is constructed directly from DEFAULT_LOCAL_MODEL.
        - local_only=True is passed to ModelClient.chat() as a second guard.
        """
        text_parts: list[str] = []
        for m in messages:
            role = m.get("role", "")
            content = m.get("content") or ""
            if content and role in ("user", "assistant"):
                # Cap individual message length so the summarization prompt
                # itself doesn't overflow for pathologically long turns.
                text_parts.append(f"{role.capitalize()}: {content[:1200]}")

        if not text_parts:
            return "(no conversation content to summarize)"

        conversation_text = "\n\n".join(text_parts)

        # Belt-and-suspenders: construct local model string directly; also
        # pass local_only=True so ModelClient enforces it independently.
        local_model = f"local/{self._config.DEFAULT_LOCAL_MODEL}"

        try:
            result = await self._llm.chat(
                messages=[
                    {
                        "role": "user",
                        "content": (
                            "Summarize this conversation excerpt into a compact paragraph. "
                            "Preserve verbatim: decisions made, commitments given, specific "
                            "facts about people, dates, or plans. Be concise.\n\n"
                            + conversation_text
                        ),
                    }
                ],
                model=local_model,
                local_only=True,  # privacy guard — ModelClient enforces this too
            )
            summary = result.get("content", "").strip()
            if not summary:
                raise ValueError("LLM returned empty summary")
            return summary
        except Exception as exc:
            logger.error("compression_summarize_failed", error=str(exc))
            # Fallback: truncated concatenation so we don't block the turn
            return " | ".join(p[:150] for p in text_parts[:4])

    async def _save_to_recall(self, messages: list[dict]) -> None:
        """Persist compressed-out turns to recall memory via pgvector.

        Nothing is truly lost — it just moves out of the active context window
        and into the searchable recall layer.
        """
        if not self._memory:
            return
        for m in messages:
            role = m.get("role", "")
            content = m.get("content") or ""
            if not content or role not in ("user", "assistant"):
                continue
            try:
                await self._memory.save_to_recall(
                    f"[Archived - {role}]: {content[:800]}",
                    importance=0.4,
                )
            except Exception as exc:
                logger.warning(
                    "archive_to_recall_failed", role=role, error=str(exc)
                )
