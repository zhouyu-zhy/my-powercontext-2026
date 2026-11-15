# Copyright (c) 2026 OceanBase.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Chunked Context Packs and the prefix-reuse delta that transports them.

A prepared context normally travels in full every turn, so repeated bytes are
paid for again and again. ``ContextPack`` splits the body into bounded, valid
UTF-8 chunks and carries a digest over exactly those chunks; ``ContextPackDelta``
then names how many leading chunks of the previous pack the receiver may keep
and which chunks follow them. A receiver holding only serialized bytes can rebuild
the target pack byte for byte, and the measured wire cost is the first complete
pack plus every later delta, including JSON, Base64, digests and fields.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from dataclasses import dataclass
from typing import Any

CONTEXT_PACK_SCHEMA = "powercontext.context-pack.v1"
CONTEXT_PACK_DELTA_SCHEMA = "powercontext.context-pack-delta.v1"
DEFAULT_CHUNK_BYTES = 512

_PACK_FIELDS = frozenset({"schema", "items", "digest", "byte_count"})
_DELTA_FIELDS = frozenset({"schema", "base_digest", "target_digest", "prefix_items", "appended"})
_DIGEST_PREFIX = "sha256:"
_HEX = frozenset("0123456789abcdef")


class ContextPackError(ValueError):
    """One pack or delta violates the wire or chunking contract."""


@dataclass(frozen=True, slots=True)
class ContextPack:
    """A complete context body as bounded chunks plus a digest over those chunks."""

    items: tuple[bytes, ...]
    digest: str

    def __post_init__(self) -> None:
        if isinstance(self.items, str) or not isinstance(self.items, tuple):
            raise ContextPackError("pack items must be a tuple of bytes")  # noqa: TRY003
        if any(not isinstance(item, bytes) or not item for item in self.items):
            raise ContextPackError("every pack chunk must be a non-empty bytes value")  # noqa: TRY003
        if not _is_digest(self.digest):
            raise ContextPackError("pack digest must be sha256:<64 lowercase hex>")  # noqa: TRY003
        if self.digest != _pack_digest(self.items):
            raise ContextPackError("pack digest does not match its items")  # noqa: TRY003

    @classmethod
    def build(cls, items: tuple[bytes, ...]) -> ContextPack:
        """Seal one tuple of chunks into a pack with its own digest."""
        chunks = tuple(items)
        return cls(items=chunks, digest=_pack_digest(chunks))

    @classmethod
    def from_content(cls, content: str | bytes, *, chunk_bytes: int = DEFAULT_CHUNK_BYTES) -> ContextPack:
        """Chunk one body deterministically, keeping every chunk valid UTF-8 and bounded."""
        return cls.build(chunk_context(content, chunk_bytes))

    @property
    def byte_count(self) -> int:
        """The exact length of this pack's own serialization."""
        return len(self.to_bytes())

    def to_bytes(self) -> bytes:
        """Serialize the pack, deriving a byte count that describes the whole payload."""
        return _with_byte_count({"schema": CONTEXT_PACK_SCHEMA, "items": _encoded(self.items), "digest": self.digest})

    @classmethod
    def from_bytes(cls, data: bytes) -> ContextPack:
        """Decode and validate one serialized pack, rejecting any tampering."""
        payload = _object(data)
        if frozenset(payload) != _PACK_FIELDS:
            raise ContextPackError("pack must carry exactly schema, items, digest and byte_count")  # noqa: TRY003
        if payload["schema"] != CONTEXT_PACK_SCHEMA:
            raise ContextPackError("pack schema is not supported")  # noqa: TRY003
        byte_count = payload["byte_count"]
        if not _is_count(byte_count) or byte_count != len(data):
            raise ContextPackError("pack byte count does not match the encoded pack")  # noqa: TRY003
        if not _is_digest(payload["digest"]):
            raise ContextPackError("pack digest must be sha256:<64 lowercase hex>")  # noqa: TRY003
        return cls(items=_decoded(payload["items"]), digest=payload["digest"])


@dataclass(frozen=True, slots=True)
class ContextPackDelta:
    """How many base chunks to keep and which target chunks replace the rest."""

    base_digest: str
    target_digest: str
    prefix_items: int
    appended: tuple[bytes, ...]

    def __post_init__(self) -> None:
        for field, value in (("base_digest", self.base_digest), ("target_digest", self.target_digest)):
            if not _is_digest(value):
                raise ContextPackError(f"delta {field} must be sha256:<64 lowercase hex>")  # noqa: TRY003
        if not _is_count(self.prefix_items):
            raise ContextPackError("delta prefix_items must be a non-negative integer")  # noqa: TRY003
        if isinstance(self.appended, str) or not isinstance(self.appended, tuple):
            raise ContextPackError("delta appended must be a tuple of bytes")  # noqa: TRY003
        if any(not isinstance(item, bytes) or not item for item in self.appended):
            raise ContextPackError("every appended chunk must be a non-empty bytes value")  # noqa: TRY003

    @classmethod
    def between(cls, base: ContextPack, target: ContextPack) -> ContextPackDelta:
        """Reuse the longest chunk prefix the two packs share and send the remainder."""
        shared = 0
        for left, right in zip(base.items, target.items, strict=False):
            if left != right:
                break
            shared += 1
        return cls(base.digest, target.digest, shared, target.items[shared:])

    @property
    def byte_count(self) -> int:
        """The exact length of this delta's own serialization, never a wire field."""
        return len(self.to_bytes())

    def to_bytes(self) -> bytes:
        return _canonical(
            {
                "schema": CONTEXT_PACK_DELTA_SCHEMA,
                "base_digest": self.base_digest,
                "target_digest": self.target_digest,
                "prefix_items": self.prefix_items,
                "appended": _encoded(self.appended),
            }
        )

    @classmethod
    def from_bytes(cls, data: bytes) -> ContextPackDelta:
        """Decode and validate one serialized delta."""
        payload = _object(data)
        if frozenset(payload) != _DELTA_FIELDS:
            raise ContextPackError(  # noqa: TRY003
                "delta must carry exactly schema, base_digest, target_digest, prefix_items and appended"
            )
        if payload["schema"] != CONTEXT_PACK_DELTA_SCHEMA:
            raise ContextPackError("delta schema is not supported")  # noqa: TRY003
        return cls(
            base_digest=payload["base_digest"],
            target_digest=payload["target_digest"],
            prefix_items=payload["prefix_items"],
            appended=_decoded(payload["appended"]),
        )


def apply_context_pack_delta(base: ContextPack, delta: ContextPackDelta) -> ContextPack:
    """Rebuild the target pack from one base pack plus the delta sent for it."""
    if not isinstance(base, ContextPack) or not isinstance(delta, ContextPackDelta):
        raise ContextPackError("a delta applies only to a validated pack")  # noqa: TRY003
    if delta.base_digest != base.digest:
        raise ContextPackError("delta base digest does not match the stored pack")  # noqa: TRY003
    if delta.prefix_items > len(base.items):
        raise ContextPackError("delta prefix exceeds the base chunk count")  # noqa: TRY003
    target = ContextPack.build(base.items[: delta.prefix_items] + delta.appended)
    if target.digest != delta.target_digest:
        raise ContextPackError("delta target digest does not match the rebuilt pack")  # noqa: TRY003
    return target


def chunk_context(content: str | bytes, chunk_bytes: int = DEFAULT_CHUNK_BYTES) -> tuple[bytes, ...]:
    """Split one body into deterministic chunks at line boundaries.

    Lines are the natural reuse unit, so a chunk ends with a newline whenever the
    limit allows; a line longer than the limit is cut into bounded valid UTF-8
    pieces. Chunk boundaries never split a character, and joining them reproduces
    the original bytes exactly.
    """
    if not _is_count(chunk_bytes) or chunk_bytes < 1:
        raise ContextPackError("chunk_bytes must be a positive integer")  # noqa: TRY003
    raw = content.encode("utf-8") if isinstance(content, str) else bytes(content)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ContextPackError("context content must be valid UTF-8") from error  # noqa: TRY003
    chunks: list[bytes] = []
    for line in _lines(text):
        encoded = line.encode("utf-8")
        if len(encoded) <= chunk_bytes:
            chunks.append(encoded)
            continue
        chunks.extend(_bounded(line, chunk_bytes))
    return tuple(chunks)


def _lines(text: str) -> list[str]:
    """Split after every newline while keeping each separator with its line."""
    lines: list[str] = []
    start = 0
    for index, char in enumerate(text):
        if char == "\n":
            lines.append(text[start : index + 1])
            start = index + 1
    if start < len(text):
        lines.append(text[start:])
    return lines


def _bounded(line: str, chunk_bytes: int) -> list[bytes]:
    """Cut one over-long line into bounded whole-character pieces."""
    pieces: list[bytes] = []
    current = ""
    size = 0
    for char in line:
        width = len(char.encode("utf-8"))
        if width > chunk_bytes:
            raise ContextPackError("a single UTF-8 character exceeds the chunk limit")  # noqa: TRY003
        if size + width > chunk_bytes:
            pieces.append(current.encode("utf-8"))
            current, size = char, width
        else:
            current += char
            size += width
    if current:
        pieces.append(current.encode("utf-8"))
    return pieces


def _canonical(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _pack_digest(items: tuple[bytes, ...]) -> str:
    unsigned = {"schema": CONTEXT_PACK_SCHEMA, "items": _encoded(items)}
    return _DIGEST_PREFIX + hashlib.sha256(_canonical(unsigned)).hexdigest()


def _with_byte_count(body: dict[str, Any]) -> bytes:
    """Emit one payload whose declared byte count describes its own length."""
    count = len(_canonical({**body, "byte_count": 0}))
    for _ in range(16):
        encoded = _canonical({**body, "byte_count": count})
        if len(encoded) == count:
            return encoded
        count = len(encoded)
    raise ContextPackError("pack byte count has no stable encoding")  # noqa: TRY003


def _encoded(items: tuple[bytes, ...]) -> list[str]:
    return [base64.b64encode(item).decode("ascii") for item in items]


def _decoded(value: Any) -> tuple[bytes, ...]:  # noqa: ANN401
    if not isinstance(value, list):
        raise ContextPackError("pack chunks must be a JSON array of Base64 strings")  # noqa: TRY003
    chunks: list[bytes] = []
    for item in value:
        if not isinstance(item, str):
            raise ContextPackError("pack chunks must be standard Base64 strings")  # noqa: TRY003
        try:
            decoded = base64.b64decode(item, validate=True)
        except (binascii.Error, ValueError) as error:
            raise ContextPackError("pack chunks must be standard Base64 strings") from error  # noqa: TRY003
        if base64.b64encode(decoded).decode("ascii") != item or not decoded:
            raise ContextPackError("pack chunks must be canonical non-empty Base64 values")  # noqa: TRY003
        chunks.append(decoded)
    return tuple(chunks)


def _object(data: bytes) -> dict[str, Any]:
    if not isinstance(data, (bytes, bytearray)):
        raise ContextPackError("pack wire input must be bytes")  # noqa: TRY003
    try:
        payload = json.loads(bytes(data))
    except ValueError as error:
        raise ContextPackError("pack wire input must be one JSON object") from error  # noqa: TRY003
    if not isinstance(payload, dict):
        raise ContextPackError("pack wire input must be one JSON object")  # noqa: TRY003
    return payload


def _is_digest(value: Any) -> bool:  # noqa: ANN401
    return (
        isinstance(value, str)
        and value.startswith(_DIGEST_PREFIX)
        and len(value) == len(_DIGEST_PREFIX) + 64
        and set(value[len(_DIGEST_PREFIX) :]) <= _HEX
    )


def _is_count(value: Any) -> bool:  # noqa: ANN401
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0
