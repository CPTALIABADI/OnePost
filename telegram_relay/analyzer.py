from __future__ import annotations

from typing import Any

from .models import Entity, MediaInfo, MessageInfo, TextContent

MEDIA_FIELDS = (
    "photo", "video", "document", "audio", "voice", "animation", "sticker",
    "video_note", "live_photo", "paid_media", "story",
)


def parse_entities(items: list[dict[str, Any]] | None) -> list[Entity]:
    return [Entity.from_dict(item) for item in (items or [])]


def parse_text(message: dict[str, Any]) -> TextContent:
    return TextContent(message.get("text"), parse_entities(message.get("entities")))


def parse_caption(message: dict[str, Any]) -> TextContent:
    return TextContent(message.get("caption"), parse_entities(message.get("caption_entities")))


def _make_media(kind: str, obj: dict[str, Any]) -> MediaInfo:
    return MediaInfo(
        kind=kind,
        file_id=obj.get("file_id"),
        file_unique_id=obj.get("file_unique_id"),
        file_name=obj.get("file_name"),
        mime_type=obj.get("mime_type"),
        file_size=obj.get("file_size"),
        width=obj.get("width"),
        height=obj.get("height"),
        duration=obj.get("duration"),
        raw=dict(obj),
    )


def detect_media(message: dict[str, Any]) -> MediaInfo | None:
    if message.get("photo"):
        return _make_media("photo", message["photo"][-1])
    for field in MEDIA_FIELDS:
        value = message.get(field)
        if value is None:
            continue
        if isinstance(value, dict):
            return _make_media(field, value)
        return MediaInfo(kind=field, raw={"value": value})
    return None


def analyze_message(message: dict[str, Any], *, update_id: int | None = None, event_type: str = "new") -> MessageInfo:
    chat = message.get("chat", {})
    return MessageInfo(
        source_update_id=update_id,
        message_id=int(message["message_id"]),
        date=int(message["date"]),
        chat_id=int(chat["id"]),
        chat_title=chat.get("title"),
        chat_username=chat.get("username"),
        text=parse_text(message),
        caption=parse_caption(message),
        media=detect_media(message),
        event_type=event_type,
        media_group_id=message.get("media_group_id"),
        has_protected_content=bool(message.get("has_protected_content", False)),
        has_media_spoiler=bool(message.get("has_media_spoiler", False)),
        show_caption_above_media=bool(message.get("show_caption_above_media", False)),
        raw_message=dict(message),
    )


def describe(info: MessageInfo) -> str:
    lines = [
        f"event={info.event_type} message_id={info.message_id} type={info.content_type}",
        f"chat={info.chat_id} title={info.chat_title!r} date={info.date}",
    ]
    if info.media_group_id:
        lines.append(f"album={info.media_group_id}")
    if info.text.value is not None:
        lines.append(f"text={info.text.value!r} entities={len(info.text.entities)}")
    if info.caption.value is not None:
        lines.append(f"caption={info.caption.value!r} entities={len(info.caption.entities)}")
    if info.media:
        lines.append(
            f"media={info.media.kind} file_id={info.media.file_id!r} "
            f"name={info.media.file_name!r} mime={info.media.mime_type!r} size={info.media.file_size}"
        )
    return " | ".join(lines)
