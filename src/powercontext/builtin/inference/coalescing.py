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

"""Cross-call request coalescing with a bounded vector cache for Embedding.

Memory writes and vector searches reach the model through calls that overlap in
time and repeat texts, so one distinct text is sent to the provider at most once
per flight: a call arriving first registers the text in the merge window, a call
arriving later joins the batch that window produced, and a later call still reuses
the successful vector from a bounded cache. Every call receives vectors in its own
input order, repeated positions included, and only the usage of the distinct
batches it waited for, so sharing never moves one call's cost onto another.

A failed batch is reported to its own waiters and is never cached, so independent
batches keep their results and the affected texts stay retryable. Cancelling one
call leaves the shared work running for the waiters that remain.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from typing import TypeAlias

from powercontext.builtin.inference.models import EmbeddingResult, EmbeddingVector, InferenceUsage
from powercontext.builtin.inference.protocols import EmbeddingModel

DEFAULT_MAX_BATCH_SIZE = 64
DEFAULT_COALESCE_MS = 5.0
DEFAULT_CACHE_SIZE = 1024

_DOCUMENT_KIND = "document"
_QUERY_KIND = "query"

# A vector is identified by its text and by the model call that produced it, because
# a provider may embed retrieval queries differently than it embeds documents.
_VectorKey: TypeAlias = tuple[str, str]
_BatchRunner: TypeAlias = Callable[[tuple[str, ...]], Awaitable[EmbeddingResult]]


@dataclass(slots=True)
class _Wish:
    """One text that calls asked for while no batch had taken it yet."""

    runner: _BatchRunner
    claims: list[tuple[dict[str, _Batch | None], str]] = field(default_factory=list)


class _Batch:
    """One provider request plus the outcome its waiters share."""

    __slots__ = ("future", "kind", "runner", "texts", "usage", "vectors")

    def __init__(self, kind: str, texts: tuple[str, ...], runner: _BatchRunner) -> None:
        self.kind = kind
        self.texts = texts
        self.runner = runner
        self.usage = InferenceUsage(requests=0)
        self.vectors: dict[str, EmbeddingVector] | None = None
        self.future: asyncio.Future[dict[str, EmbeddingVector]] = asyncio.get_running_loop().create_future()

    async def wait(self) -> dict[str, EmbeddingVector]:
        """Return the shared vectors without letting one waiter stop the work.

        The shield keeps a cancelled call from cancelling the outcome its fellow
        waiters still need, while results and failures reach every waiter.
        """

        return await asyncio.shield(self.future)

    def deliver(self, vectors: dict[str, EmbeddingVector], usage: InferenceUsage) -> None:
        """Publish a successful batch to the waiters that are still listening."""

        self.vectors = vectors
        self.usage = usage
        if not self.future.done():
            self.future.set_result(vectors)

    def abandon(self, error: BaseException) -> None:
        """Report a failure without caching it, which keeps the batch texts retryable."""

        if self.future.done():
            return
        if isinstance(error, asyncio.CancelledError):
            self.future.cancel()
        else:
            self.future.set_exception(error)


class CoalescingEmbeddingModel:
    """Embed through shared bounded batches and a cache of successful vectors."""

    def __init__(
        self,
        model: EmbeddingModel,
        *,
        max_batch_size: int = DEFAULT_MAX_BATCH_SIZE,
        coalesce_ms: float = DEFAULT_COALESCE_MS,
        cache_size: int = DEFAULT_CACHE_SIZE,
    ) -> None:
        if isinstance(max_batch_size, bool) or max_batch_size < 1:
            raise ValueError("max_batch_size must be a positive integer")  # noqa: TRY003
        if coalesce_ms < 0:
            raise ValueError("coalesce_ms must not be negative")  # noqa: TRY003
        if isinstance(cache_size, bool) or cache_size < 0:
            raise ValueError("cache_size must not be negative")  # noqa: TRY003
        self._model = model
        self.profile = model.profile
        self._max_batch_size = int(max_batch_size)
        self._coalesce_seconds = float(coalesce_ms) / 1000.0
        self._cache_size = int(cache_size)
        self._cache: OrderedDict[_VectorKey, EmbeddingVector] = OrderedDict()
        self._running: dict[_VectorKey, _Batch] = {}
        self._wanted: dict[_VectorKey, _Wish] = {}
        self._window: asyncio.Task[None] | None = None
        self._tasks: set[asyncio.Task[None]] = set()

    async def embed(self, texts: tuple[str, ...], /) -> EmbeddingResult:
        """Embed documents, sharing batches with other calls for the same texts."""

        return await self._embed(texts, _DOCUMENT_KIND, self._model.embed)

    async def embed_query(self, texts: tuple[str, ...], /) -> EmbeddingResult:
        """Embed queries, or reuse document work when the model is symmetric."""

        runner = getattr(self._model, "embed_query", None)
        if runner is None:
            return await self.embed(texts)
        return await self._embed(texts, _QUERY_KIND, runner)

    async def _embed(self, texts: tuple[str, ...], kind: str, runner: _BatchRunner) -> EmbeddingResult:
        requested = tuple(texts)
        if not requested:
            return EmbeddingResult(vectors=(), usage=InferenceUsage(requests=0))
        # Each claim maps one needed text to the batch that answers it, or to None
        # while the merge window still holds the text. Reads and claims below share
        # no suspension point with _issue, so a text is never taken twice.
        claims: dict[str, _Batch | None] = {}
        vectors: dict[str, EmbeddingVector] = {}
        for text in dict.fromkeys(requested):
            key = (kind, text)
            cached = self._reuse(key)
            if cached is not None:
                vectors[text] = cached
            elif (batch := self._running.get(key)) is not None:
                claims[text] = batch
            else:
                claims[text] = None
                self._claim(key, text, claims, runner)
        if any(batch is None for batch in claims.values()):
            if self._coalesce_seconds > 0:
                await self._await_window(claims)
            else:
                self._issue()
        batches = list(dict.fromkeys(batch for batch in claims.values() if batch is not None))
        for batch in batches:
            vectors.update(await batch.wait())
        return EmbeddingResult(
            vectors=tuple(vectors[text] for text in requested),
            usage=_total(batches),
        )

    def _claim(self, key: _VectorKey, text: str, claims: dict[str, _Batch | None], runner: _BatchRunner) -> None:
        """Record that this call is waiting for the batch that will take ``text``.

        The call's own claim mapping is registered, so the batch chosen at issue
        time becomes visible to the waiter that asked for the text.
        """

        wish = self._wanted.get(key)
        if wish is None:
            self._wanted[key] = _Wish(runner, [(claims, text)])
            return
        wish.claims.append((claims, text))

    async def _await_window(self, claims: dict[str, _Batch | None]) -> None:
        """Wait for one shared window to close so late calls reduce work."""

        while any(batch is None for batch in claims.values()):
            window = self._window
            if window is None:
                self._window = window = asyncio.create_task(self._close_window())
            await asyncio.shield(window)

    async def _close_window(self) -> None:
        try:
            await asyncio.sleep(self._coalesce_seconds)
        except asyncio.CancelledError:
            self._window = None
            self._issue()
            raise
        self._window = None
        self._issue()

    def _issue(self) -> None:
        """Give every claimed text a batch and start one request per batch."""

        wanted = self._wanted
        self._wanted = {}
        kinds: dict[str, list[_VectorKey]] = {}
        for key in wanted:
            kinds.setdefault(key[0], []).append(key)
        for kind, keys in kinds.items():
            for start in range(0, len(keys), self._max_batch_size):
                chunk = keys[start : start + self._max_batch_size]
                batch = _Batch(kind, tuple(key[1] for key in chunk), wanted[chunk[0]].runner)
                for key in chunk:
                    self._running[key] = batch
                    for claim, text in wanted[key].claims:
                        claim[text] = batch
                task = asyncio.create_task(self._execute(batch))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)

    async def _execute(self, batch: _Batch) -> None:
        try:
            result = await batch.runner(batch.texts)
        except asyncio.CancelledError:
            self._retire(batch)
            batch.abandon(asyncio.CancelledError())
            raise
        except Exception as error:  # noqa: BLE001 - the failure belongs to this batch's waiters
            self._retire(batch)
            batch.abandon(error)
            return
        delivered = tuple(result.vectors)
        if len(delivered) != len(batch.texts):
            self._retire(batch)
            batch.abandon(ValueError("embedding model must return one vector per text"))
            return
        vectors = dict(zip(batch.texts, delivered, strict=True))
        self._retire(batch)
        for text, vector in vectors.items():
            self._store((batch.kind, text), vector)
        batch.deliver(vectors, result.usage)

    def _retire(self, batch: _Batch) -> None:
        """Stop offering a finished batch to calls that arrive afterwards."""

        for text in batch.texts:
            key = (batch.kind, text)
            if self._running.get(key) is batch:
                del self._running[key]

    def _reuse(self, key: _VectorKey) -> EmbeddingVector | None:
        vector = self._cache.get(key)
        if vector is not None:
            self._cache.move_to_end(key)
        return vector

    def _store(self, key: _VectorKey, vector: EmbeddingVector) -> None:
        if self._cache_size <= 0:
            return
        self._cache[key] = vector
        self._cache.move_to_end(key)
        while len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)


def _total(batches: list[_Batch]) -> InferenceUsage:
    """Charge a call each distinct batch it waited for, keeping provider numbers."""

    if not batches:
        return InferenceUsage(requests=0)
    return InferenceUsage(
        requests=sum(batch.usage.requests for batch in batches),
        input_tokens=_tokens(batches, lambda usage: usage.input_tokens),
        output_tokens=_tokens(batches, lambda usage: usage.output_tokens),
    )


def _tokens(batches: Iterable[_Batch], reported: Callable[[InferenceUsage], int | None]) -> int | None:
    values = [value for value in (reported(batch.usage) for batch in batches) if value is not None]
    return sum(values) if values else None


__all__ = ["CoalescingEmbeddingModel", "DEFAULT_CACHE_SIZE", "DEFAULT_COALESCE_MS", "DEFAULT_MAX_BATCH_SIZE"]
