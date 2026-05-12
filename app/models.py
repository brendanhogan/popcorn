from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field

EntryType = Literal["paper", "twitter", "other"]
EntryStatus = Literal["pending", "fetching", "summarizing", "ready", "error"]


class ChatTurn(BaseModel):
    role: Literal["user", "assistant"]
    content: str
    ts: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class Entry(BaseModel):
    id: str
    url: str
    type: EntryType = "other"
    status: EntryStatus = "pending"

    title: str = ""
    title_suggestions: list[str] = Field(default_factory=list)
    rating: int = 0  # 0 = unrated, 1-3 popcorns
    notes: str = ""
    private: bool = False

    summary: str = ""
    fetched_content: str = ""  # cleaned text from trafilatura OR user paste, may be large
    fetch_error: str = ""
    attached_image_filename: str = ""  # filename under data/images/ if user dropped a screenshot
    content_source: Literal["auto", "paste", "image"] = "auto"

    chat_history: list[ChatTurn] = Field(default_factory=list)

    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def touch(self) -> None:
        self.updated_at = datetime.now(timezone.utc).isoformat()


class EntryPatch(BaseModel):
    title: str | None = None
    rating: int | None = None
    notes: str | None = None
    private: bool | None = None
    type: EntryType | None = None


class BatchRequest(BaseModel):
    urls: list[str]


class ChatRequest(BaseModel):
    message: str


class Session(BaseModel):
    id: str
    name: str
    entry_ids: list[str] = Field(default_factory=list)
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def touch(self) -> None:
        self.updated_at = datetime.now(timezone.utc).isoformat()


class SessionCreate(BaseModel):
    name: str | None = None


class SessionRename(BaseModel):
    name: str
