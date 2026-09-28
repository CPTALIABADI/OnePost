from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Entity:
    type: str
    offset: int
    length: int
    url: str | None = None
    user: dict[str, Any] | None = None
    language: str | None = None
    custom_emoji_id: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Entity":
        return cls(
            type=str(data["type"]),
            offset=int(data["offset"]),
            length=int(data["length"]),
            url=data.get("url"),
            user=data.get("user"),
            language=data.get("language"),
            custom_emoji_id=data.get("custom_emoji_id"),
            raw=dict(data),
        )

    def to_dict(self) -> dict[str, Any]:
        out = dict(self.raw)
        out.update({"type": self.type, "offset": self.offset, "length": self.length})
        if self.url is not None:
            out["url"] = self.url
        if self.user is not None:
            out["user"] = self.user
        if self.language is not None:
            out["language"] = self.language
        if self.custom_emoji_id is not None:
            out["custom_emoji_id"] = self.custom_emoji_id
        return out


@dataclass
class TextContent:
    value: str | None = None
    entities: list[Entity] = field(default_factory=list)

    def clone(self) -> "TextContent":
        return TextContent(
            value=self.value,
            entities=[
                Entity(
                    type=e.type,
                    offset=e.offset,
                    length=e.length,
                    url=e.url,
                    user=dict(e.user) if e.user else None,
                    language=e.language,
                    custom_emoji_id=e.custom_emoji_id,
                    raw=dict(e.raw),
                )
                for e in self.entities
            ],
        )


@dataclass
class MediaInfo:
    kind: str
    file_id: str | None = None
    file_unique_id: str | None = None
    file_name: str | None = None
    mime_type: str | None = None
    file_size: int | None = None
    width: int | None = None
    height: int | None = None
    duration: int | float | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class MessageInfo:
    source_update_id: int | None
    message_id: int
    date: int
    chat_id: int
    chat_title: str | None
    chat_username: str | None

    text: TextContent = field(default_factory=TextContent)
    caption: TextContent = field(default_factory=TextContent)
    media: MediaInfo | None = None

    event_type: str = "new"
    media_group_id: str | None = None
    has_protected_content: bool = False
    has_media_spoiler: bool = False
    show_caption_above_media: bool = False
    raw_message: dict[str, Any] = field(default_factory=dict)

    @property
    def content_type(self) -> str:
        if self.media:
            return self.media.kind
        if self.text.value is not None:
            return "text"
        for key in (
            "contact", "location", "venue", "poll", "dice", "game", "paid_media",
            "checklist", "story", "invoice", "successful_payment",
        ):
            if key in self.raw_message and self.raw_message[key] is not None:
                return key
        return "unknown"

    @property
    def text_value(self) -> str | None:
        return self.text.value

    @property
    def caption_value(self) -> str | None:
        return self.caption.value

    @property
    def has_text(self) -> bool:
        return self.text.value is not None

    @property
    def has_caption(self) -> bool:
        return self.caption.value is not None


@dataclass
class AlbumInfo:
    chat_id: int
    media_group_id: str
    items: list[MessageInfo]

    @property
    def message_ids(self) -> list[int]:
        return [m.message_id for m in self.items]

    @property
    def caption_item(self) -> MessageInfo | None:
        for item in self.items:
            if item.caption.value is not None:
                return item
        return self.items[0] if self.items else None

    @property
    def caption(self) -> TextContent:
        item = self.caption_item
        return item.caption.clone() if item else TextContent()
