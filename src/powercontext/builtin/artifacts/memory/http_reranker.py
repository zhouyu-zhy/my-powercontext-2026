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

"""HTTP scoring-service reranking over one global coarse candidate pool.

A rerank service limits how many candidates one request may carry, so the pool is
sent in batches. Ranks always stay the one-based positions inside the *complete*
input, and every batch contributes its scores to a single global selection, which
keeps a strong candidate in a later batch ahead of a weaker one in an earlier
batch. Selection only reorders: hit text, entries and entry versions are returned
unchanged, and the reported request count is the number of calls actually made.
"""

from __future__ import annotations

import math
from typing import Any

import httpx

from powercontext.builtin.artifacts.memory.models import MemoryHit
from powercontext.builtin.artifacts.memory.reranking import MemoryRerankDecision
from powercontext.builtin.inference import InferenceUsage

HTTP_RERANK_POLICY_ID = "http-cross-encoder-v1"
HTTP_RERANK_DEFAULT_BATCH_SIZE = 32


class HTTPMemoryReranker:
    """Score Memory candidates through a batched HTTP cross-encoder service."""

    def __init__(
        self,
        endpoint: str,
        *,
        client: httpx.AsyncClient,
        batch_size: int = HTTP_RERANK_DEFAULT_BATCH_SIZE,
        policy_id: str = HTTP_RERANK_POLICY_ID,
    ) -> None:
        if not isinstance(batch_size, int) or isinstance(batch_size, bool) or batch_size < 1:
            raise ValueError("batch_size must be a positive integer")  # noqa: TRY003
        _require_text("endpoint", endpoint)
        _require_text("policy_id", policy_id)
        self.endpoint = endpoint
        self.batch_size = batch_size
        self.policy_id = policy_id
        self._client = client

    async def rerank(
        self,
        query: str,
        candidates: tuple[MemoryHit, ...],
        limit: int,
        /,
    ) -> MemoryRerankDecision:
        """Select the best ``limit`` original ranks using complete-pool service scores."""

        candidate_count = len(candidates)
        if candidate_count < 1:
            raise ValueError("rerank requires at least one candidate")  # noqa: TRY003
        if limit < 1 or limit > candidate_count:
            raise ValueError("rerank limit must be between one and the candidate count")  # noqa: TRY003
        scores: dict[int, float] = {}
        requests = 0
        for start in range(1, candidate_count + 1, self.batch_size):
            ranks = tuple(range(start, min(start + self.batch_size, candidate_count + 1)))
            scores.update(await self._score(query, candidates, ranks))
            requests += 1
        ordered = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
        return MemoryRerankDecision(
            selected_ranks=tuple(rank for rank, _ in ordered[:limit]),
            usage=InferenceUsage(requests=requests),
        )

    async def _score(
        self,
        query: str,
        candidates: tuple[MemoryHit, ...],
        ranks: tuple[int, ...],
    ) -> dict[int, float]:
        """Send exactly one batch and validate that it covers every rank once."""

        payload = {
            "query": query,
            "candidates": [{"rank": rank, "text": candidates[rank - 1].text} for rank in ranks],
        }
        response = await self._client.post(self.endpoint, json=payload)
        response.raise_for_status()
        return _observed_scores(response.json(), ranks)


def _require_text(field: str, value: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{field} must be a non-empty trimmed string")  # noqa: TRY003
    return value


def _observed_scores(payload: Any, ranks: tuple[int, ...]) -> dict[int, float]:  # noqa: ANN401
    if not isinstance(payload, dict) or set(payload) != {"scores"}:
        raise ValueError("rerank response must contain exactly one scores array")  # noqa: TRY003
    records = payload["scores"]
    if not isinstance(records, list):
        raise ValueError("rerank scores must be a JSON array")  # noqa: TRY003
    expected = set(ranks)
    observed: dict[int, float] = {}
    for record in records:
        if not isinstance(record, dict) or set(record) != {"rank", "score"}:
            raise ValueError("each rerank score must contain exactly rank and score")  # noqa: TRY003
        rank, score = record["rank"], record["score"]
        if not isinstance(rank, int) or isinstance(rank, bool) or rank not in expected:
            raise ValueError("rerank score rank must be a rank of the current batch")  # noqa: TRY003
        if rank in observed:
            raise ValueError("rerank score ranks must appear exactly once")  # noqa: TRY003
        if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score):
            raise ValueError("rerank score must be a finite number")  # noqa: TRY003
        observed[rank] = float(score)
    if set(observed) != expected:
        raise ValueError("rerank scores must cover every rank of the current batch")  # noqa: TRY003
    return observed


__all__ = [
    "HTTP_RERANK_DEFAULT_BATCH_SIZE",
    "HTTP_RERANK_POLICY_ID",
    "HTTPMemoryReranker",
]
