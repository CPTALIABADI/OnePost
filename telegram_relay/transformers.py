from __future__ import annotations

from abc import ABC, abstractmethod
import re
from typing import Callable

from .models import Entity, MessageInfo, TextContent


def _u16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _py_to_u16(text: str, index: int) -> int:
    return _u16_len(text[:index])


def _apply_regex(content: TextContent, pattern: str, replacement: str, *, flags: int = 0) -> TextContent:
    old = content.value
    if old is None:
        return content

    regex = re.compile(pattern, flags)
    matches = list(regex.finditer(old))
    if not matches:
        return content

    # Build the new text and a piecewise mapping from old UTF-16 positions to
    # new UTF-16 positions. A mapping breakpoint is (old_end, new_end).
    pieces: list[str] = []
    breakpoints: list[tuple[int, int, int, int]] = []
    cursor = 0
    new_u16 = 0
    for m in matches:
        s, e = m.span()
        unchanged = old[cursor:s]
        pieces.append(unchanged)
        new_u16 += _u16_len(unchanged)
        old_s_u16 = _py_to_u16(old, s)
        old_e_u16 = _py_to_u16(old, e)
        repl = m.expand(replacement)
        pieces.append(repl)
        repl_u16 = _u16_len(repl)
        breakpoints.append((old_s_u16, old_e_u16, new_u16, new_u16 + repl_u16))
        new_u16 += repl_u16
        cursor = e
    tail = old[cursor:]
    pieces.append(tail)
    new_value = "".join(pieces)

    def map_point(point: int) -> int:
        delta = 0
        for old_s, old_e, new_s, new_e in breakpoints:
            if point < old_s:
                break
            if point >= old_e:
                delta = new_e - old_e
                continue
            # Point lies inside a replaced region: map to its beginning.
            return new_s
        return point + delta

    mapped: list[Entity] = []
    for entity in content.entities:
        s, e = entity.offset, entity.offset + entity.length
        overlapping = [bp for bp in breakpoints if s < bp[1] and e > bp[0]]
        if overlapping:
            # Preserve an unaffected prefix where possible, otherwise remove
            # the entity. This is deliberately conservative: invalid entity
            # ranges are worse than losing formatting on changed text.
            first = overlapping[0]
            prefix_end = min(e, first[0])
            if prefix_end > s:
                copy = Entity(**{**entity.__dict__})
                copy.offset = map_point(s)
                copy.length = map_point(prefix_end) - map_point(s)
                if copy.length > 0:
                    mapped.append(copy)
            continue

        copy = Entity(**{**entity.__dict__})
        copy.offset = map_point(s)
        copy.length = map_point(e) - map_point(s)
        if copy.length > 0:
            mapped.append(copy)

    return TextContent(new_value, mapped)


class MessageTransformer(ABC):
    @abstractmethod
    def transform(self, message: MessageInfo) -> MessageInfo:
        raise NotImplementedError


class TransformerPipeline:
    def __init__(self, transformers: list[MessageTransformer] | None = None):
        self.transformers = transformers or []

    def process(self, message: MessageInfo) -> MessageInfo:
        for transformer in self.transformers:
            message = transformer.transform(message)
        return message


class FunctionTransformer(MessageTransformer):
    def __init__(self, func: Callable[[MessageInfo], MessageInfo]):
        self.func = func

    def transform(self, message: MessageInfo) -> MessageInfo:
        return self.func(message)


class ReplaceTextTransformer(MessageTransformer):
    def __init__(self, pattern: str, replacement: str, *, flags: int = 0):
        self.pattern = pattern
        self.replacement = replacement
        self.flags = flags

    def transform(self, message: MessageInfo) -> MessageInfo:
        message.text = _apply_regex(message.text, self.pattern, self.replacement, flags=self.flags)
        message.caption = _apply_regex(message.caption, self.pattern, self.replacement, flags=self.flags)
        return message


class RemoveTelegramLinksTransformer(MessageTransformer):
    _pattern = r"(?:https?://)?(?:t\.me|telegram\.me)/[^\s)]+"

    def transform(self, message: MessageInfo) -> MessageInfo:
        message.text = _apply_regex(message.text, self._pattern, "")
        message.caption = _apply_regex(message.caption, self._pattern, "")
        return message
