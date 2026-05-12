from __future__ import annotations

import base64
import json
import os
from typing import Iterable

from anthropic import AsyncAnthropic

from .models import ChatTurn

MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")

_client = AsyncAnthropic()

_SYSTEM_PROMPT = """You are helping a researcher process a link from their biweekly reading queue. The content of the link is included below between the --- markers.

- When asked for a summary, write 3-5 sentences. Be technical and concrete. Lead with the key claim or finding. No preamble like "This article discusses..." — just state the substance.
- When asked for title suggestions, propose 3 distinct, concise candidates. Each short (under 60 characters), substantive (not clickbait).
- For follow-up questions, answer based on what's in the content. If something isn't in the content, say so explicitly rather than speculating."""

_TITLE_SCHEMA = {
    "type": "object",
    "properties": {
        "titles": {
            "type": "array",
            "items": {"type": "string"},
        }
    },
    "required": ["titles"],
    "additionalProperties": False,
}


def _system_blocks(content: str) -> list[dict]:
    return [
        {"type": "text", "text": _SYSTEM_PROMPT},
        {
            "type": "text",
            "text": f"---\n{content}\n---",
            "cache_control": {"type": "ephemeral"},
        },
    ]


async def summarize(content: str) -> str:
    response = await _client.messages.create(
        model=MODEL,
        max_tokens=600,
        system=_system_blocks(content),
        messages=[{"role": "user", "content": "Write a 3-5 sentence summary of this content."}],
    )
    return next((b.text for b in response.content if b.type == "text"), "").strip()


async def suggest_titles(content: str) -> list[str]:
    response = await _client.messages.create(
        model=MODEL,
        max_tokens=400,
        system=_system_blocks(content),
        messages=[
            {
                "role": "user",
                "content": (
                    "Propose exactly 3 distinct, concise title options for this content. "
                    "Each under 60 characters, substantive (not clickbait). "
                    "Return JSON of the form {\"titles\": [\"...\", \"...\", \"...\"]}."
                ),
            }
        ],
        output_config={"format": {"type": "json_schema", "schema": _TITLE_SCHEMA}},
    )
    text = next((b.text for b in response.content if b.type == "text"), "")
    try:
        data = json.loads(text)
        titles = [t.strip() for t in data.get("titles", []) if t and t.strip()]
        return titles[:3]
    except (json.JSONDecodeError, AttributeError):
        return []


async def extract_text_from_image(image_bytes: bytes, media_type: str) -> str:
    b64 = base64.standard_b64encode(image_bytes).decode("utf-8")
    response = await _client.messages.create(
        model=MODEL,
        max_tokens=4000,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {"type": "base64", "media_type": media_type, "data": b64},
                    },
                    {
                        "type": "text",
                        "text": (
                            "Transcribe the visible text from this image faithfully. "
                            "Preserve structure (paragraphs, line breaks, lists). For social media "
                            "posts, include the author handle/name and the full post text; if there "
                            "are multiple posts in a thread or replies in view, include them all in "
                            "the order shown. Output only the transcription — no preamble, no "
                            "commentary, no '[image of...]' descriptions."
                        ),
                    },
                ],
            }
        ],
    )
    return next((b.text for b in response.content if b.type == "text"), "").strip()


async def chat(content: str, history: Iterable[ChatTurn], message: str) -> str:
    messages: list[dict] = [{"role": t.role, "content": t.content} for t in history]
    messages.append({"role": "user", "content": message})

    response = await _client.messages.create(
        model=MODEL,
        max_tokens=2000,
        system=_system_blocks(content),
        messages=messages,
    )
    return next((b.text for b in response.content if b.type == "text"), "").strip()
