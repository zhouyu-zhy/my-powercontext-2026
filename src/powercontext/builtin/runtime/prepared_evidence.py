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

"""Rank original Memory text against one query and pack it into a byte budget.

The output charges one citation per delivered span, so this module scores *sentences* but spends
bytes on *contiguous runs*. A unit that touches an already selected run only pays for its own
characters, which is why growth runs after relevance and why a long sentence is offered at clause
granularity: both keep the budget on evidence instead of on citations.

Pure and offline: no model, no service, and no per-request knowledge beyond the query itself.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass

# A span shorter than this is a numbering or an abbreviation boundary, not a sentence.
_MIN_SENTENCE_CHARS = 26
# Long sentences are only worth re-splitting when the pieces stay readable on their own.
_MIN_CLAUSE_CHARS = 48
_LONG_SENTENCE_CHARS = 200
# Character shingles recover the morphological variants a literal term match misses
# ("calibrated" / "calibration"), so they carry a minority of the score.
_FUZZY_WEIGHT = 0.6
_WHOLE_QUERY_BONUS = 0.2
_WINDOW_BONUS = 0.15
_WINDOW_SLACK = 3
_RANK_DECAY = 0.05
_MIN_SUBSTRING_TERM = 5
_MIN_SUBSTRING_PART = 4
_IDF_EPSILON = 0.5

# The scoring tokeniser this task measures recall with: lowercase English words and digit runs.
_ASCII_TERM = re.compile(r"[a-z]+|\d+")
# Written as escapes so the module source stays ASCII even where a script does not.
_CJK_RANGES = "぀-ヿ㐀-䶿一-鿿가-힯"
_CJK_RUN = re.compile("[" + _CJK_RANGES + "]+")
_NON_TERM = re.compile("[^0-9a-z" + _CJK_RANGES + "]+")
_SENTENCE_BREAK = re.compile(r"[.!?…]+[\"')\]]*\s+|[^\S\n]*\n+[^\S\n]*")
_CLAUSE_BREAK = re.compile(
    r"[;:]\s+|,\s+|\s+(?=(?:although|because|but|however|therefore|whereas|while)\b)",
)


@dataclass(frozen=True, slots=True)
class EvidenceUnit:
    """One selectable half-open character interval of one candidate body."""

    entry: int
    start: int
    end: int
    score: float


@dataclass(frozen=True, slots=True)
class EvidenceEntry:
    """One candidate body, partitioned into adjacent units ordered by position."""

    units: tuple[EvidenceUnit, ...]


@dataclass(frozen=True, slots=True)
class EvidenceSpan:
    """One delivered interval: ``entry`` selects the body, ``start``/``end`` the exact text."""

    entry: int
    start: int
    end: int


SpanCost = Callable[[Sequence[EvidenceSpan]], int]


def plan(query: str, texts: Sequence[str]) -> tuple[EvidenceEntry, ...]:
    """Partition each candidate body into scored units, keeping the candidate order as a prior."""

    sentences = tuple(_tile(text, _SENTENCE_BREAK, _MIN_SENTENCE_CHARS) for text in texts)
    corpus = tuple(
        frozenset(_terms(text[start:end]))
        for text, spans in zip(texts, sentences, strict=True)
        for start, end in spans
    )
    analysis = _QueryAnalysis.of(query, corpus)
    entries: list[EvidenceEntry] = []
    for index, (text, spans) in enumerate(zip(texts, sentences, strict=True)):
        prior = 1.0 / (1.0 + _RANK_DECAY * index)
        pieces: list[tuple[int, int]] = []
        for start, end in spans:
            clauses = (
                _tile(text[start:end], _CLAUSE_BREAK, _MIN_CLAUSE_CHARS)
                if end - start > _LONG_SENTENCE_CHARS
                else ()
            )
            if len(clauses) > 1:
                pieces.extend((start + left, start + right) for left, right in clauses)
            else:
                pieces.append((start, end))
        entries.append(
            EvidenceEntry(
                units=tuple(
                    EvidenceUnit(index, left, right, analysis.score(text[left:right]) * prior)
                    for left, right in pieces
                    if right > left
                )
            )
        )
    return tuple(entries)


def select_spans(
    entries: Sequence[EvidenceEntry],
    *,
    budget: int,
    cost: SpanCost,
) -> tuple[EvidenceSpan, ...]:
    """Choose the intervals to deliver, measured against the real rendering of the output.

    Relevant units are offered best-first, so the evidence the query asks for is never displaced by
    cheap filler. The budget then grows what is already selected, because a neighbour of a delivered
    span pays only for its own characters while a unit starting a new span also pays its citation,
    and only after that does it fall back to text the query did not match. Since every acceptance is
    charged exactly, the returned spans are the ones :meth:`cost` prices within ``budget``.
    """

    selected: dict[int, list[tuple[int, int]]] = {index: [] for index in range(len(entries))}

    def priced(state: dict[int, list[tuple[int, int]]]) -> int:
        return cost(
            tuple(
                EvidenceSpan(index, start, end)
                for index in sorted(state)
                for start, end in state[index]
            )
        )

    def merge(unit: EvidenceUnit) -> list[tuple[int, int]] | None:
        """Return the intervals one entry would hold after ``unit``, or nothing if it changes none."""

        merged = _union(selected[unit.entry], (unit.start, unit.end))
        return merged if merged != selected[unit.entry] else None

    def trial(unit: EvidenceUnit, merged: list[tuple[int, int]]) -> dict[int, list[tuple[int, int]]]:
        return {index: (merged if index == unit.entry else spans) for index, spans in selected.items()}

    def accept(unit: EvidenceUnit) -> bool:
        merged = merge(unit)
        if merged is None:
            return False
        if priced(trial(unit, merged)) > budget:
            return False
        selected[unit.entry] = merged
        return True

    units = sorted(
        (unit for entry in entries for unit in entry.units),
        key=lambda unit: (-unit.score, unit.entry, unit.start),
    )
    for unit in units:
        if unit.score > 0.0:
            accept(unit)
    for _ in range(2):
        _grow(entries, selected, accept)
        for unit in units:
            if unit.score <= 0.0:
                accept(unit)
    return tuple(
        EvidenceSpan(index, start, end)
        for index in sorted(selected)
        for start, end in selected[index]
    )


@dataclass(frozen=True)
class _QueryAnalysis:
    terms: tuple[str, ...]
    weights: dict[str, float]
    total: float
    shingles: frozenset[str]

    @classmethod
    def of(cls, query: str, corpus: Sequence[frozenset[str]]) -> _QueryAnalysis:
        """Weight each query term by how few candidate sentences use it, so common words stop winning."""

        count = len(corpus)
        idf: dict[str, float] = {}
        if count:
            documents: dict[str, int] = {}
            for terms in corpus:
                for term in terms:
                    documents[term] = documents.get(term, 0) + 1
            idf = {
                term: math.log(1.0 + (count - seen + _IDF_EPSILON) / (seen + _IDF_EPSILON))
                for term, seen in documents.items()
            }
        ceiling = math.log(1.0 + 2.0 * count)
        terms = tuple(dict.fromkeys(_terms(query)))
        weights = {term: idf.get(term, ceiling) for term in terms}
        return cls(
            terms=terms,
            weights=weights,
            total=sum(weights.values()),
            shingles=_shingles(query),
        )

    def score(self, text: str) -> float:
        if self.total <= 0.0:
            return 0.0
        terms = _terms(text)
        present = frozenset(terms)
        matched = {term for term in self.terms if term in present}
        unmatched = (
            term
            for term in self.terms
            if term not in matched and len(term) >= _MIN_SUBSTRING_TERM
        )
        for term in unmatched:
            if any(
                other != term and len(other) >= _MIN_SUBSTRING_PART and (term in other or other in term)
                for other in present
            ):
                matched.add(term)
        covered = sum(self.weights[term] for term in matched)
        if covered <= 0.0:
            return 0.0
        score = covered / self.total + _FUZZY_WEIGHT * _dice(self.shingles, _shingles(text))
        if len(matched) == len(self.terms) and len(self.terms) > 1:
            score += _WHOLE_QUERY_BONUS
            if _tightest_window(terms, matched) <= len(self.terms) + _WINDOW_SLACK:
                score += _WINDOW_BONUS
        return score


def _tile(text: str, breaks: re.Pattern[str], minimum: int) -> tuple[tuple[int, int], ...]:
    """Split ``text`` at every boundary, charging separators to the span they end.

    The spans stay contiguous and every one of them keeps its own characters, so a caller can read
    a unit back as ``text[start:end]`` and the union of neighbours covers the original exactly.
    """

    spans: list[tuple[int, int]] = []
    start = 0
    for match in breaks.finditer(text):
        if match.end() - start >= minimum:
            spans.append((start, match.end()))
            start = match.end()
    if start < len(text):
        spans.append((start, len(text)))
    return tuple(span for span in spans if text[span[0] : span[1]].strip())


def _terms(text: str) -> tuple[str, ...]:
    """Extract comparable terms, treating each CJK character and character pair as one term."""

    lowered = text.lower()
    terms = list(_ASCII_TERM.findall(_CJK_RUN.sub(" ", lowered)))
    for run in _CJK_RUN.findall(lowered):
        terms.extend(run)
        terms.extend(run[index : index + 2] for index in range(len(run) - 1))
    return tuple(terms)


def _shingles(text: str) -> frozenset[str]:
    compact = _NON_TERM.sub("", text.lower())
    if len(compact) < 4:
        return frozenset({compact}) if compact else frozenset()
    return frozenset(compact[index : index + 4] for index in range(len(compact) - 3))


def _dice(left: frozenset[str], right: frozenset[str]) -> float:
    if not left or not right:
        return 0.0
    return 2.0 * len(left & right) / (len(left) + len(right))


def _tightest_window(terms: Sequence[str], wanted: set[str]) -> int:
    need = len(wanted)
    counts: dict[str, int] = {}
    have = 0
    best = len(terms)
    left = 0
    for right, term in enumerate(terms, start=1):
        if term in wanted:
            counts[term] = counts.get(term, 0) + 1
            if counts[term] == 1:
                have += 1
        while have == need:
            best = min(best, right - left)
            leading = terms[left]
            if leading in wanted:
                counts[leading] -= 1
                if counts[leading] == 0:
                    have -= 1
            left += 1
    return best


def _grow(
    entries: Sequence[EvidenceEntry],
    selected: dict[int, list[tuple[int, int]]],
    accept: Callable[[EvidenceUnit], bool],
) -> None:
    """Extend delivered spans into their neighbours while the budget prefers continuity."""

    while True:
        options: list[EvidenceUnit] = []
        for index, entry in enumerate(entries):
            for start, end in selected[index]:
                options.extend(
                    unit
                    for unit in entry.units
                    if (unit.end == start or unit.start == end) and not _covered(selected[index], unit)
                )
        if not options:
            return
        # A unit that closes a gap replaces the citation it would otherwise need, so prefer the
        # most relevant neighbour and let a cheaper one through when that one does not fit.
        options.sort(key=lambda unit: (-unit.score, unit.end - unit.start, unit.entry, unit.start))
        if not any(accept(unit) for unit in options):
            return


def _covered(spans: Sequence[tuple[int, int]], unit: EvidenceUnit) -> bool:
    return any(start <= unit.start and unit.end <= end for start, end in spans)


def _union(spans: Sequence[tuple[int, int]], span: tuple[int, int]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for start, end in sorted([*spans, span]):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


__all__ = ("EvidenceEntry", "EvidenceSpan", "EvidenceUnit", "plan", "select_spans")
