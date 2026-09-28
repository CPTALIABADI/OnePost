from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any
import logging
import json
import re

from .media import guess_mime

log = logging.getLogger(__name__)
from .models import AlbumInfo, Entity, MessageInfo, TextContent


_BALE_SPECIALS = r"\\*_[]()~`>#+-=|{}.!"


def _escape_bale(text: str) -> str:
    specials = _BALE_SPECIALS.rstrip("\n")
    return "".join(("\\" + ch) if ch in specials else ch for ch in text)


def _u16_to_py(text: str, offset: int) -> int:
    used = 0
    for i, ch in enumerate(text):
        if used >= offset:
            return i
        used += 2 if ord(ch) > 0xFFFF else 1
    return len(text)


def telegram_to_bale_markdown(content: TextContent) -> str:
    text = content.value or ""
    if not text:
        return ""
    if not content.entities:
        return _escape_bale(text)

    wrappers = {
        "bold": ("*", "*"),
        "italic": ("_", "_"),
        "underline": ("__", "__"),
        "strikethrough": ("~", "~"),
        "code": ("`", "`"),
        "pre": ("```", "```"),
    }
    spans: list[tuple[int, int, Entity]] = []
    for entity in content.entities:
        if entity.type not in wrappers and entity.type not in {"text_link", "text_mention", "url", "mention"}:
            continue
        s = _u16_to_py(text, entity.offset)
        e = _u16_to_py(text, entity.offset + entity.length)
        if e > s:
            spans.append((s, e, entity))

    # Conservative rendering for nested entities: choose outer/then inner spans,
    # escaping the segment once. It is preferable to preserve text rather than
    # create malformed destination markup.
    insertions: list[tuple[int, str]] = []
    for s, e, entity in spans:
        segment = _escape_bale(text[s:e])
        if entity.type in wrappers:
            a, b = wrappers[entity.type]
            insertions.append((s, a))
            insertions.append((e, b))
        else:
            url = entity.url
            if entity.type == "mention" and not url:
                # A mention without a resolvable username cannot be faithfully
                # converted to Bale's link syntax; leave text unchanged.
                continue
            if entity.type == "text_mention" and not url:
                continue
            insertions.append((s, "[" + segment + "](" + url + ")" if url else ""))
            insertions.append((e, "" if not url else ""))

    # Rebuild by segments. Formatting is best-effort; destinations have different
    # entity grammars, while the underlying text remains unchanged.
    events: dict[int, list[str]] = {}
    for pos, token in insertions:
        events.setdefault(pos, []).append(token)

    out: list[str] = []
    cursor = 0
    for pos in sorted(events):
        out.append(_escape_bale(text[cursor:pos]))
        out.extend(events[pos])
        cursor = pos
    out.append(_escape_bale(text[cursor:]))
    rendered = "".join(out)

    # text_link replacement above already includes its segment, so prevent the
    # original segment being duplicated by rebuilding those spans separately.
    if any(e.type in {"text_link", "text_mention"} for _, _, e in spans):
        # Reliable general path: apply link spans right-to-left on plain escaped
        # text while formatting wrappers are handled independently below.
        base = text
        link_spans = [s for s in spans if s[2].type in {"text_link", "text_mention"} and s[2].url]
        for s, e, entity in sorted(link_spans, reverse=True):
            base = base[:s] + f"[{_escape_bale(base[s:e])}]({entity.url})" + base[e:]
        rendered = _escape_bale(text)
        for s, e, entity in sorted(link_spans, reverse=True):
            rendered = rendered[:s] + f"[{_escape_bale(text[s:e])}]({entity.url})" + rendered[e:]
        # Re-apply simple wrappers on the rendered text; offsets may be affected
        # by links, so only apply wrappers that do not overlap a link.
        wrapper_spans = [s for s in spans if s[2].type in wrappers]
        for s, e, entity in sorted(wrapper_spans, reverse=True):
            a, b = wrappers[entity.type]
            segment = rendered[s:e]
            rendered = rendered[:s] + a + segment + b + rendered[e:]
    else:
        rendered = "".join(out)
    return rendered


class DestinationError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = True):
        super().__init__(message)
        self.retryable = retryable


@dataclass
class SendResult:
    message_id: str
    raw: dict[str, Any] | None = None


class Destination(ABC):
    name: str
    supports_albums: bool = False

    @abstractmethod
    def validate(self) -> None:
        raise NotImplementedError

    @abstractmethod
    def send(self, message: MessageInfo, media_path: Path | None = None) -> SendResult:
        raise NotImplementedError

    def send_album(self, album: AlbumInfo, media_paths: list[Path]) -> list[SendResult]:
        raise DestinationError(f"Destination {self.name} does not support albums", retryable=False)

    @abstractmethod
    def edit(self, message: MessageInfo, destination_message_id: str, media_path: Path | None = None) -> SendResult:
        raise NotImplementedError

    @abstractmethod
    def delete(self, destination_message_id: str) -> None:
        raise NotImplementedError


class BaleDestination(Destination):
    name = "bale"
    supports_albums = True

    def __init__(self, token: str, chat_id: str | int, session, timeout: int = 60):
        self.token = token
        self.chat_id = chat_id
        self.session = session
        self.timeout = timeout
        self.base = f"https://tapi.bale.ai/bot{token}"

    def _call(self, method: str, data: dict[str, Any] | None = None, files=None) -> Any:
        try:
            response = self.session.post(
                f"{self.base}/{method}",
                data=data if files else None,
                json=data if not files else None,
                files=files,
                timeout=self.timeout,
            )
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:
            raise DestinationError(f"Bale {method} request failed: {exc}") from exc
        if not payload.get("ok"):
            code = int(payload.get("error_code", 0) or 0)
            raise DestinationError(f"Bale {method} failed: {payload}", retryable=code >= 500 or code == 429)
        return payload.get("result")

    def validate(self) -> None:
        self._call("getMe")

    @staticmethod
    def _message_id(result: Any) -> str:
        if isinstance(result, dict) and "message_id" in result:
            return str(result["message_id"])
        raise DestinationError(f"Unexpected Bale response: {result!r}", retryable=False)

    def _single_media_payload(self, message: MessageInfo, media_path: Path) -> tuple[str, dict[str, Any], str]:
        kind = message.media.kind if message.media else "document"
        method = {
            "photo": "sendPhoto",
            "video": "sendVideo",
            "audio": "sendAudio",
            "voice": "sendVoice",
            "animation": "sendAnimation",
            "document": "sendDocument",
        }.get(kind, "sendDocument")
        field = {
            "sendPhoto": "photo", "sendVideo": "video", "sendAudio": "audio",
            "sendVoice": "voice", "sendAnimation": "animation", "sendDocument": "document",
        }[method]
        attach_name = media_path.name
        data: dict[str, Any] = {"chat_id": self.chat_id, field: f"attach://{attach_name}"}
        if message.caption.value is not None:
            data["caption"] = telegram_to_bale_markdown(message.caption)
        return method, data, field

    def send(self, message: MessageInfo, media_path: Path | None = None) -> SendResult:
        if message.media is None:
            if message.text.value is None:
                raise DestinationError(f"Bale cannot send unsupported Telegram message {message.content_type}", retryable=False)
            result = self._call("sendMessage", {"chat_id": self.chat_id, "text": telegram_to_bale_markdown(message.text)})
            return SendResult(self._message_id(result), result)
        if media_path is None:
            raise DestinationError("Bale media message requires a materialized file", retryable=False)
        method, data, field = self._single_media_payload(message, media_path)
        with media_path.open("rb") as fh:
            result = self._call(method, data, files={media_path.name: (media_path.name, fh, guess_mime(media_path, message.media))})
        return SendResult(self._message_id(result), result)

    def send_album(self, album: AlbumInfo, media_paths: list[Path]) -> list[SendResult]:
        if len(media_paths) != len(album.items):
            raise DestinationError("Bale album path count mismatch", retryable=False)
        entries = []
        open_files = []
        files = {}
        try:
            caption_item = album.caption_item
            for idx, (item, path) in enumerate(zip(album.items, media_paths)):
                media_type = "photo" if item.media and item.media.kind == "photo" else "video"
                name = path.name
                part = {"type": media_type, "media": f"attach://{name}"}
                if caption_item is item and item.caption.value is not None:
                    part["caption"] = telegram_to_bale_markdown(item.caption)
                entries.append(part)
                fh = path.open("rb")
                open_files.append(fh)
                files[name] = (name, fh, guess_mime(path, item.media))
            result = self._call("sendMediaGroup", {"chat_id": self.chat_id, "media": json.dumps(entries, ensure_ascii=False)}, files=files)
        finally:
            for fh in open_files:
                fh.close()
        if not isinstance(result, list) or len(result) != len(album.items):
            raise DestinationError(f"Unexpected Bale sendMediaGroup response: {result!r}", retryable=False)
        return [SendResult(self._message_id(item), item) for item in result]

    def edit(self, message: MessageInfo, destination_message_id: str, media_path: Path | None = None) -> SendResult:
        try:
            msg_id = int(destination_message_id)
        except ValueError as exc:
            raise DestinationError("Bale destination message id is not an integer", retryable=False) from exc
        if message.media is None and message.text.value is not None:
            result = self._call("editMessageText", {"chat_id": self.chat_id, "message_id": msg_id, "text": telegram_to_bale_markdown(message.text)})
            return SendResult(self._message_id(result), result)
        if message.media is not None and message.caption.value is not None:
            result = self._call("editMessageCaption", {"chat_id": self.chat_id, "message_id": msg_id, "caption": telegram_to_bale_markdown(message.caption)})
            return SendResult(self._message_id(result), result)
        raise DestinationError("Bale cannot edit this message shape", retryable=False)

    def delete(self, destination_message_id: str) -> None:
        self._call("deleteMessage", {"chat_id": self.chat_id, "message_id": int(destination_message_id)})


class RubikaDestination(Destination):
    name = "rubika"
    supports_albums = False  # No album/group-send method is documented by Rubika's Bot API.

    FILE_TYPE = {
        "photo": "Image",
        "video": "Video",
        "audio": "Music",
        "voice": "Voice",
        "animation": "Gif",
        "document": "File",
        "sticker": "File",
        "video_note": "Video",
    }

    def __init__(self, token: str, chat_id: str, session, timeout: int = 60):
        self.token = token
        self.chat_id = chat_id
        self.session = session
        self.timeout = timeout
        self.base = f"https://botapi.rubika.ir/v3/{token}"

    def _call(self, method: str, data: dict[str, Any] | None = None) -> Any:
        try:
            response = self.session.post(f"{self.base}/{method}", json=data or {}, timeout=self.timeout)
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:
            raise DestinationError(f"Rubika {method} request failed: {exc}") from exc
        if payload.get("status") not in (None, "OK"):
            status = str(payload.get("status"))
            dev_message = str(payload.get("dev_message", ""))
            permanent = status == "INVALID_INPUT" and bool(re.search(r"invalid[_ ]format", dev_message, re.IGNORECASE))
            raise DestinationError(f"Rubika {method} failed: {payload}", retryable=not permanent)
        return payload.get("data", payload.get("result", payload))

    def validate(self) -> None:
        self._call("getMe")

    @staticmethod
    def _message_id(result: Any) -> str:
        if isinstance(result, dict):
            value = result.get("message_id") or result.get("messageId")
            if value is not None:
                return str(value)
        raise DestinationError(f"Unexpected Rubika response: {result!r}", retryable=False)

    def _request_upload_url(self, file_type: str) -> str:
        request = self._call("requestSendFile", {"type": file_type})
        upload_url = request.get("upload_url") if isinstance(request, dict) else None
        if not upload_url:
            raise DestinationError(
                f"Rubika requestSendFile returned no upload_url for {file_type}: {request!r}",
                retryable=False,
            )
        return str(upload_url)

    def _upload_once(self, media_path: Path, message: MessageInfo, file_type: str) -> str:
        upload_url = self._request_upload_url(file_type)
        try:
            with media_path.open("rb") as fh:
                response = self.session.post(
                    upload_url,
                    files={"file": (media_path.name, fh, guess_mime(media_path, message.media))},
                    timeout=self.timeout,
                )
                response.raise_for_status()
                payload = response.json()
        except Exception as exc:
            raise DestinationError(f"Rubika file upload request failed: {exc}") from exc
        file_id = payload.get("file_id") or payload.get("data", {}).get("file_id")
        if not file_id:
            status = str(payload.get("status", ""))
            dev = str(payload.get("dev_message", ""))
            permanent = status == "INVALID_INPUT" and bool(re.search(r"invalid[_ ]format", dev, re.IGNORECASE))
            raise DestinationError(
                f"Rubika upload returned no file_id: {payload!r}",
                retryable=not permanent,
            )
        return str(file_id)

    def _upload(self, media_path: Path, message: MessageInfo) -> tuple[str, str]:
        if not message.media:
            raise DestinationError("Missing media metadata", retryable=False)
        preferred = self.FILE_TYPE.get(message.media.kind)
        if not preferred:
            raise DestinationError(
                f"Rubika has no mapped file type for {message.media.kind}", retryable=False
            )
        try:
            return self._upload_once(media_path, message, preferred), preferred
        except DestinationError as exc:
            # Preserve the exact source bytes whenever possible. Some audio
            # containers (notably M4A in practice) can be rejected by Rubika's
            # Music classifier. Sending the same bytes as a generic File avoids
            # transcoding and therefore preserves the original media.
            if (
                message.media.kind in {"audio", "voice"}
                and not exc.retryable
                and bool(re.search(r"invalid[_ ]format", str(exc), re.IGNORECASE))
                and preferred != "File"
            ):
                log.warning(
                    "Rubika rejected %s as %s; retrying the same bytes as generic File",
                    media_path.name, preferred,
                )
                return self._upload_once(media_path, message, "File"), "File"
            raise

    @staticmethod
    def _metadata(content: TextContent) -> dict[str, Any] | None:
        # Rubika uses UTF-16-style indexing in its API examples as character indexes;
        # retain only entities that can be represented without guessing.
        parts = []
        for e in content.entities:
            mapping = {
                "bold": "Bold",
                "italic": "Italic",
                "underline": "Underline",
                "strikethrough": "Strike",
                "text_link": "Link",
            }
            kind = mapping.get(e.type)
            if not kind:
                continue
            part = {"type": kind, "from_index": e.offset, "length": e.length}
            if e.url:
                part["link_url"] = e.url
            parts.append(part)
        return {"meta_data_parts": parts} if parts else None

    def send(self, message: MessageInfo, media_path: Path | None = None) -> SendResult:
        if message.media is None:
            if message.text.value is None:
                raise DestinationError(f"Rubika cannot send unsupported Telegram message {message.content_type}", retryable=False)
            data: dict[str, Any] = {"chat_id": self.chat_id, "text": message.text.value}
            metadata = self._metadata(message.text)
            if metadata:
                data["metadata"] = metadata
            result = self._call("sendMessage", data)
            return SendResult(self._message_id(result), result)

        if media_path is None:
            raise DestinationError("Rubika media message requires a materialized file", retryable=False)
        file_id, rubika_file_type = self._upload(media_path, message)
        data: dict[str, Any] = {"chat_id": self.chat_id, "file_id": file_id}
        if message.caption.value is not None:
            data["text"] = message.caption.value
            metadata = self._metadata(message.caption)
            if metadata:
                data["metadata"] = metadata
        result = self._call("sendFile", data)
        return SendResult(self._message_id(result), result)

    def edit(self, message: MessageInfo, destination_message_id: str, media_path: Path | None = None) -> SendResult:
        text = message.caption.value if message.media else message.text.value
        if text is None:
            raise DestinationError("Rubika edit has no text", retryable=False)
        data = {"chat_id": self.chat_id, "message_id": destination_message_id, "text": text}
        content = message.caption if message.media else message.text
        metadata = self._metadata(content)
        if metadata:
            data["metadata"] = metadata
        result = self._call("editMessageText", data)
        return SendResult(self._message_id(result), result)

    def delete(self, destination_message_id: str) -> None:
        self._call("deleteMessage", {"chat_id": self.chat_id, "message_id": destination_message_id})
