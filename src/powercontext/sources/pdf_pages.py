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

"""One physical PDF page as one durable, citable Source.

A filing is read as the file itself: every physical page becomes a ``CAPTURED`` Source whose
identity is the document plus the digest of the exact bytes plus the page position, so a citation
names a page of a revision rather than a chunk of text, and a re-issued filing cannot silently
repoint an older reference.

Retrieval keeps the whole document in one Memory and asks for evidence page by page. A filing
question is long and phrased the way an analyst speaks, while the page that answers it is phrased
the way a filing is written, so one lexical query over the difference of the two wordings is not a
reliable way to find it. ``search_pdf_pages`` therefore asks the same Memory several narrow questions
derived from the one it was given and trusts those questions to agree: a page several of them reach is
a page the filing itself connects, where a page one question reaches alone has merely happened to share
a word or two. The agreement is then read together with the shape of the page it was found on, because a
statement prints its figures in a column while the writing around them quotes one, and the difference
says which page an analyst would turn to. Only the pages that could still be delivered are read back,
which is how their physical positions become known — the position two revisions of one filing can hold
twice. Everything here is
offline: no model, no service, and nothing about a particular document or question is baked in.
"""

from __future__ import annotations

import hashlib
import inspect
import io
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from pydantic import BaseModel, Field, field_validator
from pypdf import PdfReader
from pypdf.errors import PyPdfError

from powercontext.artifacts import MemoryCitation
from powercontext.limits import MAX_SOURCE_ID_LENGTH
from powercontext.sources.definitions import AdapterSourceDefinition
from powercontext.sources.models import Source, SourceMaterialization
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY, TextEvidence

PDF_PAGE_SOURCE_NAME = "pdf-page"
PDF_IDENTITY_SCHEME = "pdf-page"

# Candidate retrieval is a bounded number of statements: the evidence words, the same words with
# numeric runs split, a few adjacent word pairs and a few single words.
_MAX_PAIR_VARIANTS = 6
_MAX_SINGLE_VARIANTS = 4
_MAX_VARIANT_TERMS = 10
_MAX_VARIANTS = 12
# Each query contributes its own ranking, so the pool is sized per query rather than overall.
_CANDIDATES_PER_QUERY = 80
# Agreement is counted as a vote rather than a placing: a page several derived questions reach is a page
# the filing itself connects, and the rank one question gave it is damped rather than trusted, because
# the ordering is left to the shape of the page. The best rank of a query is worth about three times its
# worst, which is a preference rather than a verdict.
_FUSION_CONSTANT = 40.0
# A statement prints its figures in a column and a paragraph quotes one, so the rate at which a page
# states amounts separates the page an analyst counted from the pages that discussed it. The weight is
# a share of the best agreement score, and the saturation is the rate at which that share is fully
# earned — a dozen to a hundred words, which is where a table begins and prose does not.
_AMOUNT_WEIGHT = 1.0
_AMOUNT_SATURATION = 12.0
# Reading a page back names the physical page a citation points at, which is the one thing the searched
# text does not carry, so the reads stop at the pages that could still be delivered: three times as
# many as were asked for leaves room for the duplicates two revisions of one filing leave behind.
_PAGES_PER_REQUEST = 3

_TERMS = re.compile(r"[a-z]+|\d+")
_ALPHA_RUN = re.compile(r"[a-z]+")
# One amount as a filing prints it: grouped by commas, or long enough to be a figure on its own. A
# bare four-digit year is neither, because a filing dates its prose in the same words it counts in a
# column, and only the column is the evidence.
_AMOUNT = re.compile(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|(?<![\w.])(?!19|20)\d{4}(?![\w.])|\d{5,}")

# Words that ask for evidence without naming any. They are removed from derived queries so that a
# filing's own wording decides which pages match, not the shape of the sentence that asked.
_FUNCTION_WORDS = frozenset(
    """
    a about after all and any are as at be been before but by can could did do does each for from
    give has have how if in into is it its may no not of on or our please per should so than that
    the their them then there these they this those to us was were we what when where which who
    will with would you your
    """.split()
)
# The instruction phrasing FinanceBench-style questions carry, listed by its distinctive words
# rather than by whole sentences: it describes how to answer, never what the filing says. Section
# names such as "statement" stay, because a filing uses them to say what a page is.
_INSTRUCTION_WORDS = frozenset(
    """
    accordingly answer assuming calculate computed define definition details directly explain
    explicitly give ignoring millions perspective please quantify respond suppose
    temporarily unit usd using
    """.split()
)


class PDFDocumentInput(BaseModel):
    """One whole PDF file, read offline together with the identity it is filed under."""

    document_id: str
    content: bytes

    @field_validator("document_id")
    @classmethod
    def validate_document_id(cls, value: str) -> str:
        return _require_document_id(value)


class PDFPageInput(BaseModel):
    """One physical page of one identified document revision."""

    document_id: str
    document_sha256: str
    page_number: int = Field(ge=1)
    text: str

    @field_validator("document_id")
    @classmethod
    def validate_document_id(cls, value: str) -> str:
        return _require_document_id(value)

    @field_validator("document_sha256")
    @classmethod
    def validate_document_sha256(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("document_sha256 must not be blank")  # noqa: TRY003
        return value


class PDFPageSource(Source):
    """Durable identity and extracted body of one captured page."""

    source_id: str
    document_id: str
    document_sha256: str
    page_number: int = Field(ge=1)
    text: str

    @field_validator("document_id")
    @classmethod
    def validate_document_id(cls, value: str) -> str:
        return _require_document_id(value)


def _require_document_id(value: str) -> str:
    if not value.strip():
        raise ValueError("document_id must not be blank")  # noqa: TRY003
    return value


def pdf_page_source_id(document_id: str, document_sha256: str, page_number: int) -> str:
    """Build the identity of one page of one document revision.

    The digest is what separates a filing from its revision, so it is carried in the identity rather
    than derived from stored state: two revisions of the same ``document_id`` never share a page id,
    and an older citation keeps naming the page it was written for.
    """

    _require_document_id(document_id)
    if page_number < 1:
        raise ValueError("page_number must count physical pages from one")  # noqa: TRY003
    if not document_sha256.strip():
        raise ValueError("document_sha256 must not be blank")  # noqa: TRY003
    identifier = (
        f"{PDF_IDENTITY_SCHEME}:{quote(document_id, safe='')}:{document_sha256}:{page_number}"
    )
    if len(identifier) > MAX_SOURCE_ID_LENGTH:
        # An oversized document id is folded to a digest of itself: the identity stays short, and
        # two documents share an identity only if their ids hash alike.
        digest = hashlib.sha256(document_id.encode("utf-8")).hexdigest()
        identifier = f"{PDF_IDENTITY_SCHEME}:{digest}:{document_sha256}:{page_number}"
    if len(identifier) > MAX_SOURCE_ID_LENGTH:
        raise ValueError(f"Source identity must not exceed {MAX_SOURCE_ID_LENGTH} characters")  # noqa: TRY003
    return identifier


def _open_pdf(content: bytes) -> PdfReader:
    """Read the whole file structure before any page is handed back."""

    try:
        reader = PdfReader(io.BytesIO(content))
    except (PyPdfError, OSError, ValueError) as error:
        raise ValueError(f"PDF document could not be read: {error}") from error
    if reader.is_encrypted and not reader.decrypt(""):
        raise ValueError("PDF document requires a user password")  # noqa: TRY003
    return reader


def _page_text(page: Any, page_number: int) -> str:
    try:
        return page.extract_text() or ""
    except (PyPdfError, OSError, ValueError) as error:
        raise ValueError(f"PDF page {page_number} could not be extracted: {error}") from error


def parse_pdf_document(value: PDFDocumentInput) -> tuple[PDFPageInput, ...]:
    """Extract every physical page of one PDF, in file order, or nothing at all.

    Pages are numbered from one over the file's own page list, so a citation resolves against the
    original document rather than against a printed folio. Blank pages are returned with empty text
    because they are physical pages of the filing. The digest is taken over the bytes that were
    given, which is what lets a caller recover the exact revision a page came from.
    """

    reader = _open_pdf(value.content)
    digest = hashlib.sha256(value.content).hexdigest()
    try:
        page_count = len(reader.pages)
    except (PyPdfError, OSError, ValueError) as error:
        raise ValueError(f"PDF document could not be read: {error}") from error
    pages = tuple(
        PDFPageInput(
            document_id=value.document_id,
            document_sha256=digest,
            page_number=page_number,
            text=_page_text(page, page_number),
        )
        for page_number, page in enumerate(reader.pages, start=1)
    )
    if len(pages) != page_count:  # pragma: no cover - a reader that loses pages mid-iteration
        raise ValueError("PDF document did not yield every physical page")  # noqa: TRY003
    return pages


class PDFPageSourceAdapter:
    """Turn one validated page into a Source and read that page back without re-parsing."""

    input_class = PDFPageInput
    name = PDF_PAGE_SOURCE_NAME
    source_class = PDFPageSource

    async def resolve(self, value: PDFPageInput, /) -> PDFPageSource:
        source_id = pdf_page_source_id(value.document_id, value.document_sha256, value.page_number)
        return PDFPageSource(
            name=source_id,
            materialization=SourceMaterialization.CAPTURED,
            source_id=source_id,
            document_id=value.document_id,
            document_sha256=value.document_sha256,
            page_number=value.page_number,
            text=value.text,
        )

    async def read(self, source: PDFPageSource, /) -> PDFPageInput:
        return PDFPageInput(
            document_id=source.document_id,
            document_sha256=source.document_sha256,
            page_number=source.page_number,
            text=source.text,
        )


class PDFPageTextEvidenceProjection:
    """Expose one page as searchable text that still names the document it belongs to.

    The document id is part of the projected content because a page of a filing is only evidence for
    that filing: retrieval, admission and a reviewer all need the id where the words are.
    """

    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class = PDFPageSource
    output_class: type[BaseModel] = TextEvidence

    def project(self, source: PDFPageSource, /) -> TextEvidence:
        return TextEvidence(
            source_type=PDF_PAGE_SOURCE_NAME,
            source_id=source.source_id,
            content=f"{source.document_id}\n{source.text}",
            metadata={
                "document_id": source.document_id,
                "document_sha256": source.document_sha256,
                "page_number": source.page_number,
            },
        )


PDF_PAGE_SOURCE_ADAPTER = PDFPageSourceAdapter()
PDF_PAGE_SOURCE_DEFINITION: AdapterSourceDefinition[PDFPageInput, PDFPageSource, PDFPageInput] = (
    AdapterSourceDefinition(PDF_PAGE_SOURCE_ADAPTER, projections=(PDFPageTextEvidenceProjection(),))
)


@dataclass(slots=True)
class _Page:
    """One page while the evidence that ranks it is still being gathered."""

    hit: Any
    best_rank: int
    fusion: float = 0.0
    boost: float = 0.0
    location: tuple[str, int] | None = None
    amounts: float = 0.0


def _amount_density(text: str) -> float:
    """How many stated amounts one page prints per hundred of its words.

    A filing writes an amount with its separators, so "12,187" is one figure however the word tokenizer
    cuts it. A statement prints a column of them and a paragraph quotes one, which is a difference of
    shape rather than of subject — and shape is what tells the statement page from the thirty pages that
    discuss the same subject. The text a hit carries is the stored page with the document id in front of
    it, so the shape of a candidate is known as it arrives.
    """

    _, _, body = (text or "").partition("\n")
    words = len(body.split())
    return 0.0 if not words else 100.0 * len(_AMOUNT.findall(body)) / words


def _split_runs(terms: Iterable[str]) -> tuple[str, ...]:
    """Add the parts of an alphanumeric run, since a filing writes "FY2022" and "2022"."""

    parts: list[str] = []
    for term in terms:
        parts.append(term)
        if _ALPHA_RUN.fullmatch(term) is None:
            parts.extend(_TERMS.findall(term))
    return tuple(dict.fromkeys(part for part in parts if part))


def _evidence_terms(question: str) -> tuple[str, ...]:
    """Keep the words that name evidence and drop the words that ask for it."""

    terms = tuple(_TERMS.findall(question.casefold()))
    meaningful = tuple(
        dict.fromkeys(
            term
            for term in terms
            if term not in _FUNCTION_WORDS and term not in _INSTRUCTION_WORDS
        )
    )
    return meaningful or terms


def _query_variants(question: str) -> tuple[str, ...]:
    """Derive the narrow queries one filing question implies.

    A whole sentence is a poor query because its filler is what a filing repeats everywhere, while
    the few words that name the evidence are what only a part of the document says. The narrow forms
    therefore carry the question's own wording: the evidence words alone reach a page that discusses
    them in any order, adjacent pairs preserve multi-word terms whose halves are common on their own,
    and single words reach a page that uses one decisive term. Alphanumeric runs are also split, so a
    question about a fiscal year still finds the page that prints it inside a heading.
    """

    terms = _evidence_terms(question)
    variants: list[str] = []
    if terms:
        variants.append(" ".join(terms[:_MAX_VARIANT_TERMS]))
        variants.extend(
            " ".join(terms[index : index + 2])
            for index in range(min(len(terms) - 1, _MAX_PAIR_VARIANTS))
        )
        distinctive = sorted(terms, key=lambda term: (-len(term), term))
        variants.extend(distinctive[:_MAX_SINGLE_VARIANTS])
        variants.append(" ".join(_split_runs(terms)[:_MAX_VARIANT_TERMS]))
    return tuple(dict.fromkeys(variant for variant in variants if variant.strip()))[:_MAX_VARIANTS]


def _key(hit: Any) -> tuple[str, str]:
    return (hit.memory_ref.artifact_id, hit.entry_id)


def _citation(hit: Any) -> MemoryCitation:
    return MemoryCitation(
        memory_ref=hit.memory_ref,
        entry_id=hit.entry_id,
        entry_version_id=hit.entry_version_id,
    )


async def _call(memory: Any, method: str, request: str, /, **fields: Any) -> Any:
    """Call one Memory method through whichever shape it offers.

    The scoped application takes a request model; the Artifact service takes the same fields as
    keyword arguments. The signature decides which one to call, so a failure inside the call still
    surfaces as that failure instead of being read as a shape mismatch.
    """

    from powercontext.builtin import runtime  # noqa: PLC0415

    try:
        parameters = list(inspect.signature(getattr(memory, method)).parameters.values())
    except (TypeError, ValueError):  # pragma: no cover - a decorated method that hides its signature
        parameters = []
    if parameters and parameters[0].name != "request":
        return await getattr(memory, method)(**fields)
    return await getattr(memory, method)(getattr(runtime, request)(**fields))


async def _search(memory: Any, query: str, limit: int) -> tuple[Any, ...]:
    page = await _call(
        memory, "search", "SearchMemoryRequest", query=query, limit=limit, mode="auto"
    )
    return tuple(getattr(page, "hits", None) or ())


async def _locate(memory: Any, page: _Page) -> None:
    """Read one candidate back to the physical page it names.

    The position is not in the searched text — a projection carries the document id and the page body
    — so it is taken from the stored Source reference, which is exactly what makes a citation
    checkable. Two revisions of one filing hold the same page twice, and a reviewer needs to know
    which one they are reading, so this is the step that says whether two hits are one page. A
    candidate that cannot be read keeps the identity its hit gave it.
    """

    try:
        record = await _call(memory, "get", "GetMemoryEntryRequest", citation=_citation(page.hit))
    except Exception:  # noqa: BLE001 - a candidate that cannot be read is still a candidate
        return
    entry = getattr(record, "entry", None)
    if entry is None:
        return
    number = 0
    for reference in getattr(entry, "sources", ()):
        if reference.source_type != PDF_PAGE_SOURCE_NAME:
            continue
        _, _, tail = reference.source_id.rpartition(":")
        if tail.isdigit():
            number = int(tail)
    if not number:
        return
    document_id = str(getattr(entry, "text", "") or "").partition("\n")[0]
    page.location = (document_id.strip(), number)


def _with_amounts(pages: Iterable[_Page], strongest: float) -> None:
    """Raise the pages that state amounts at the rate a table states them.

    An analyst asks what a filing's figures add up to, and the page holding those figures is the page
    that prints them — a paragraph quoting one number says the same words with none of the columns.
    The rate saturates because a page already printing a column of figures is the page being looked
    for, however many more digits its neighbours print.
    """

    for page in pages:
        page.boost += _AMOUNT_WEIGHT * strongest * page.amounts / (page.amounts + _AMOUNT_SATURATION)


def _order(page: _Page) -> tuple[float, float, int, tuple[str, str]]:
    return (-(page.fusion + page.boost), -page.fusion, page.best_rank, _key(page.hit))


async def search_pdf_pages(
    memory: Any, question: str, limit: int = 10
) -> tuple[MemoryCitation, ...]:
    """Return up to ``limit`` distinct page citations, best evidence first.

    Every citation comes from a Memory hit, so what a caller reads back is the stored page rather
    than something assembled here. Two entries can name the same physical page of two revisions of
    one filing, and a reviewer needs to know which page they are reading, so a citation is kept only
    when its document and page position differ from every citation before it. The result never
    exceeds ``limit``.
    """

    if limit < 1:
        raise ValueError("limit must be at least one page")  # noqa: TRY003
    variants = _query_variants(question)
    if not variants:
        return ()
    pool_size = max(_CANDIDATES_PER_QUERY, limit * 2)
    pages: dict[tuple[str, str], _Page] = {}
    for variant in variants:
        for rank, hit in enumerate(await _search(memory, variant, pool_size)):
            key = _key(hit)
            found = pages.get(key)
            if found is None:
                text = str(getattr(hit, "text", "") or "")
                found = _Page(hit=hit, best_rank=rank, amounts=_amount_density(text))
                pages[key] = found
            found.fusion += 1.0 / (_FUSION_CONSTANT + rank)
            if rank < found.best_rank:
                found.hit, found.best_rank = hit, rank
    if not pages:
        return ()
    _with_amounts(pages.values(), max(page.fusion for page in pages.values()))
    ranked = sorted(pages.values(), key=_order)
    for page in ranked[: limit * _PAGES_PER_REQUEST]:
        await _locate(memory, page)
    seen: set[tuple[str, str] | tuple[str, int]] = set()
    citations: list[MemoryCitation] = []
    for page in ranked:
        location: tuple[str, str] | tuple[str, int] = page.location or _key(page.hit)
        if location in seen:
            continue
        seen.add(location)
        citations.append(_citation(page.hit))
        if len(citations) == limit:
            break
    return tuple(citations)


__all__ = [
    "PDF_PAGE_SOURCE_DEFINITION",
    "PDF_PAGE_SOURCE_NAME",
    "PDFDocumentInput",
    "PDFPageInput",
    "PDFPageSource",
    "parse_pdf_document",
    "pdf_page_source_id",
    "search_pdf_pages",
]
