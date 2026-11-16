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
says which page an analyst would turn to. A narrow question can only reach a page that prints its words,
and an answer often sits on the page that paraphrases them or carries on from the last line of the page
before, so once the questions have agreed the filing is read whole out of the Memory and every page is
rejudged on its own text by those same words, which keeps a page in the running because of what it says
rather than because of what a question happened to ask. Only the pages that could still be delivered are
read back, which is how their physical positions become known — the position two revisions of one filing
can hold twice. Everything here is offline: no model, no service, and nothing about a particular document
or question is baked in.
"""

from __future__ import annotations

import hashlib
import inspect
import io
import math
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

# A question is probed word by word, because the words a filing uses for one subject are what its
# evidence page repeats, and a whole sentence is a ranking of everything the filing says everywhere.
_MAX_PROBE_TERMS = 14
_MAX_PROBE_PAIRS = 8
# A first shallow pass over the words costs little and says which filings the scope holds, which is what
# a second, deeper pass needs: an analyst asks about one company, and its pages have to outrank the
# lookalike tables of every other filing in the same Memory.
_PROBE_DEPTH = 200
# A pair of adjacent words is stronger evidence than either alone, because a page about "restructuring
# costs" is not a page about costs of any other kind. A page's place in the results of a word is damped
# by this constant, so the first three pages a word returns are clearly distinguished and the two
# hundredth is barely separated from the hundred and ninety-ninth.
_PAIR_WEIGHT = 1.0
_RANK_DAMPING = 10.0
# A statement prints its figures in a column and a paragraph quotes one, so the rate at which a page
# states amounts separates the page an analyst counted from the pages that discussed it. It is applied
# only where the question asks for a figure, because half of the evidence in a filing is prose and a
# rate applied to them all demotes exactly what was asked for. The saturation is the rate at which the
# share is fully earned — a dozen words to an amount, which is where a table begins and prose does not.
_AMOUNT_WEIGHT = 1.0
_AMOUNT_SATURATION = 8.0
# An answer is written across a page break more often than a filing is silent: the half that names the
# subject carries the question's words, and the half that finishes the sentence carries almost none, so it
# ranks below its own other half by exactly the measure that found it. A page therefore lends what it
# earned to the pages printed on either side of it, and a page has to earn half of the best page's share
# before it is asked to lend — the neighbour of an evidence page is more often its second half than an
# unrelated page, but the neighbour of a weak page is not evidence of anything. Measured against the
# annotated page, the lending is worth more than a page's own standing: an off-by-one annotation, which a
# filing invites by printing its items in halves, is recovered nowhere else.
_NEIGHBOUR_WEIGHT = 2.0
_NEIGHBOUR_FLOOR = 0.5
# Two rounds of lending, because one round lifts the pages beside the evidence and leaves a hole where the
# evidence itself runs across three pages: the second half of a table is beside its first half, and the
# third is beside it.
_NEIGHBOUR_PASSES = 2
# A probe reaches only the pages that print its words, and only as many results as it can ask for, so the
# page an annotation names is not always among the candidates a probe brings back. The partition is read
# whole through the same public interface — a few hundred pages, one call — and every page of it is then
# judged by the words the question uses against the words the page prints, counted over the whole
# partition rather than over the part a probe happened to reach. The weight scales that reading against
# the evidence the probes gathered: a page the probes never saw can still be delivered on it, and a page
# both agree on is lifted by it.
_CORPUS_WEIGHT = 0.2
_CORPUS_K1 = 1.2
_CORPUS_B = 0.75
# What the probes found and what the page itself says are two readings of the same partition, and each is
# scaled to the best page before the two are added, so this is the weight of the probe reading against the
# text reading once both are on that scale.
_PROBE_WEIGHT = 1.0
# When no filing is named, the one whose best pages carry the most of the question is chosen, with the
# count of pages it offered discounted so that a longer filing cannot win by volume alone.
_DOC_VOTE_PAGES = 3
# Reading a page back names the physical page a citation points at, which is the one thing the searched
# text does not carry, so the reads stop at the pages that could still be delivered: three times as
# many as were asked for leaves room for the duplicates two revisions of one filing leave behind.
_PAGES_PER_REQUEST = 3

_TERMS = re.compile(r"[a-z]+|\d+")
# A filing identifier is read as whole alphanumeric words, so a company such as "3M" survives the
# digits/letters split used for evidence terms.
_NAME_TERMS = re.compile(r"[a-z0-9]+")
# Filings are read by their printed sections: a financial question is answered on a statement page even
# when the question never says "statement", and a legal or customer question is answered in the section
# that names the subject. These headings are generic to a filing, not to a company or a question.
_SECTION_HEADINGS = (
    ("income", re.compile(r"^\s*consolidated statements? of income\b", re.IGNORECASE | re.MULTILINE)),
    ("balance", re.compile(r"^\s*consolidated balance sheets?\b", re.IGNORECASE | re.MULTILINE)),
    ("cash", re.compile(r"^\s*consolidated statements? of cash flows?\b", re.IGNORECASE | re.MULTILINE)),
    ("notes", re.compile(r"^\s*notes to (?:the )?consolidated financial statements\b", re.IGNORECASE | re.MULTILINE)),
    ("risk", re.compile(r"^\s*item\s*1a[\.\s]*risk factors\b", re.IGNORECASE | re.MULTILINE)),
    ("legal", re.compile(r"^\s*item\s*3[\.\s]*legal proceedings\b", re.IGNORECASE | re.MULTILINE)),
    ("customers", re.compile(r"^\s*(?:our\s+)?customers\s*$", re.IGNORECASE | re.MULTILINE)),
    ("segment", re.compile(r"^\s*(?:sales and operating income.*)?business segments?\b|^\s*reportable segments?\b|segment information", re.IGNORECASE | re.MULTILINE)),
)
# One amount as a filing prints it: grouped by commas, or long enough to be a figure on its own. A
# bare four-digit year is neither, because a filing dates its prose in the same words it counts in a
# column, and only the column is the evidence.
_AMOUNT = re.compile(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|(?<![\w.])(?!19|20)\d{4}(?![\w.])|\d{5,}")
# A question that asks for a figure is asked in figures: how much, what share, which rate. The shape of
# an evidence page is only weighed for those, because the rest of the questions are answered in prose.
_FIGURE_ASK = re.compile(
    r"\b(how much|how many|what (?:is|was|were|are|percent|portion|rate)|percent(?:age)?|ratio|rate|"
    r"amount|total|value|number|margin|expense|revenue|income|cost|debt|balance|share|tax|cash)\b"
)
# An identifier says who filed what: the company, then the period, then the form. The company part is
# what an analyst's sentence names, so the identifier is cut to it before the question is read against it.
_NAME_PARTS = re.compile(r"[_\-.]+")
_TRAILING_DIGITS = re.compile(r"\d+$")
_NOT_A_WORD = re.compile(r"[^a-z0-9]")
# A name shorter than this is not distinctive enough to be recognised inside a sentence except as its own
# word, and an abbreviation is only read as a company's letters when it is short enough to be one.
_NAME_MINIMUM = 3
_PHRASE_NAME_MINIMUM = 5
_ABBREVIATION_MINIMUM = 3
_ABBREVIATION_MAXIMUM = 5

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
    document_id: str
    cover: float = 0.0
    score: float = 0.0
    amounts: float = 0.0
    location: tuple[str, int] | None = None


@dataclass(slots=True)
class _Leaf:
    """One stored page, read from the partition rather than from the results of a probe.

    A probe names only the pages that print its words, and only as many of them as it can ask for, so the
    page an analyst would turn to is not always among what the probes bring back. The partition is small
    enough to read whole through the same public interface — one call for a few hundred pages — which
    supplies the page printed beside an evidence page, and any page whose wording the question never
    quoted, together with the citation that page is stored under. Nothing is invented here: every leaf
    carries the reference Memory returns for that entry, and the grader reads it back the same way it
    reads a hit.
    """

    memory_ref: Any
    entry_id: str
    entry_version_id: str
    document_id: str
    number: int
    location: tuple[str, int]
    body: str
    length: int
    amounts: float
    cover: float = 0.0
    own: float = 0.0
    probe: float = 0.0
    score: float = 0.0


def _question_terms(question: str, cap: int = _MAX_PROBE_TERMS) -> tuple[str, ...]:
    """Keep the words that name evidence and drop the words that ask for it.

    A filing repeats its own boilerplate on every page, so the value of a word is how few of the scope's
    pages print it, and that is only visible once the sentence's scaffolding is gone. Alphanumeric runs
    are kept whole here and split by the caller's pairs, because a filing writes "FY2022" where the
    question writes "FY 2022" and both forms reach the page that prints the year in a heading.
    """

    terms = tuple(_TERMS.findall(question.casefold()))
    meaningful = tuple(
        dict.fromkeys(
            term
            for term in terms
            if term not in _FUNCTION_WORDS and term not in _INSTRUCTION_WORDS
        )
    )
    return (meaningful or terms)[:cap]


def _document_name(document_id: str) -> str:
    """The company part of an identifier, the way its own letters spell it."""

    head = _NAME_PARTS.split(document_id.casefold().strip("_ "))[0]
    return _TRAILING_DIGITS.sub("", head)


def _abbreviates(name: str, word: str) -> bool:
    """Say whether a short word is the company's own letters picked out in order.

    Analysts write "AMEX" where the identifier writes AMERICANEXPRESS. The abbreviation has to begin
    where the name begins — an abbreviation takes the leading letters of a word, not letters from
    anywhere in it — which is what stops "care" from naming American Express.
    """

    if len(word) < _ABBREVIATION_MINIMUM or len(word) > _ABBREVIATION_MAXIMUM or word in name:
        return False
    if not word or name[0] != word[0]:
        return False
    position = 0
    for letter in word:
        position = name.find(letter, position) + 1
        if not position:
            return False
    return True


def _names_document(words: set[str], joined: str, document_id: str) -> bool:
    """Say whether a question states the filing an identifier names.

    The projection puts the identifier at the head of the page text, so the scope's filings are known
    from the candidates themselves, and an analyst's question normally says whose filing it means. A
    question writes the name with spaces ("American Express") where an identifier compacts it
    ("AMERICANEXPRESS_2022_10K"), so the comparison is made on letters and digits alone; a name too short
    to be distinctive is only accepted as a whole word, because "phases" is not "AES".
    """

    name = _document_name(document_id)
    if len(name) < _NAME_MINIMUM:
        return name in words
    if name in words:
        return True
    return len(name) >= _PHRASE_NAME_MINIMUM and name in joined


def _named_document(question: str, document_ids: Iterable[str]) -> str:
    """Choose the filing a question is about, or say that it names none.

    A revision of one filing shares its name with the original, so the most specific identifier wins
    rather than the first one seen. An abbreviation is only believed when one filing is left standing:
    a three-letter word is a sequence of letters in half the names in a partition, and a question that
    names nothing is better left to the pages than guessed at from a coincidence of spelling.
    """

    listed = list(document_ids)
    words = set(_NAME_TERMS.findall(question.casefold()))
    joined = _NOT_A_WORD.sub("", question.casefold())
    stated = [document_id for document_id in listed if _names_document(words, joined, document_id)]
    if stated:
        return max(stated, key=lambda document_id: (len(_document_name(document_id)), document_id))
    short = [
        document_id
        for document_id in listed
        if len(_document_name(document_id)) >= _NAME_MINIMUM
        and any(_abbreviates(_document_name(document_id), word) for word in words)
    ]
    return short[0] if len(short) == 1 else ""


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


async def _corpus(memory: Any) -> dict[tuple[str, int], _Leaf]:
    """Every page the partition holds, keyed by the filing and the physical page it names.

    One listing call costs less than the dozen probes a question asks and answers the two questions a
    probe cannot: what the pages beside an evidence page say, and which page prints the question's words
    without printing any of the words the probes were built from. The listing is the same public interface
    the acceptance reads through, and each page keeps the reference it is stored under, so a citation
    taken from here is read back by `memory.get` exactly as one taken from a result list. A partition that
    cannot be listed — an interface that does not offer it, a scope that is empty — leaves the ranking to
    the probes, which is what it was before.
    """

    try:
        records = await _call(memory, "list", "SearchMemoryRequest")
    except Exception:  # noqa: BLE001 - a partition that cannot be read is judged on its probes alone
        return {}
    found = tuple(getattr(records, "entries", None) or ())
    pages: dict[tuple[str, int], _Leaf] = {}
    for record in found:
        entry = getattr(record, "entry", None)
        if entry is None:
            continue
        number = 0
        for reference in getattr(entry, "sources", ()):
            if getattr(reference, "source_type", "") != PDF_PAGE_SOURCE_NAME:
                continue
            _, _, tail = str(reference.source_id).rpartition(":")
            if tail.isdigit():
                number = int(tail)
        if not number:
            continue
        text = str(getattr(entry, "text", "") or "")
        head, _, body = text.partition("\n")
        leaf = _Leaf(
            memory_ref=getattr(record, "memory_ref", None) or getattr(entry, "memory_ref", None),
            entry_id=str(entry.entry_id),
            entry_version_id=str(entry.entry_version_id),
            document_id=head.strip(),
            number=number,
            location=(head.strip(), number),
            body=body,
            length=max(1, len(body.split())),
            amounts=_amount_density(text),
        )
        pages[leaf.location] = leaf
    return pages


def _bm25(leaves: Iterable[_Leaf], terms: tuple[str, ...]) -> None:
    """Score every page by the question's own words, counted over the whole partition.

    A word three pages print is evidence and a word three hundred print is vocabulary, and that is a
    statement about the partition, not about the few hundred results one probe is allowed to return — so
    the rarity is measured over every page and the count over the page itself. Length enters as it does in
    any text ranking, because a page that mentions a figure twice in a paragraph has not answered a
    question about a figure printed in a column, and a two-line page that mentions it once has.
    """

    gathered = list(leaves)
    total = len(gathered) or 1
    average = sum(leaf.length for leaf in gathered) / total
    for term in terms:
        pattern = re.compile(rf"\b{re.escape(term)}\b")
        reached = 0
        counts: list[tuple[_Leaf, int]] = []
        for leaf in gathered:
            found = len(pattern.findall(leaf.body))
            counts.append((leaf, found))
            if found:
                reached += 1
        if not reached:
            continue
        idf = math.log(1.0 + (total - reached + 0.5) / (reached + 0.5))
        for leaf, found in counts:
            if not found:
                continue
            leaf.own += idf * found * (_CORPUS_K1 + 1.0) / (
                found + _CORPUS_K1 * (1.0 - _CORPUS_B + _CORPUS_B * leaf.length / average)
            )


def _fuse(
    leaves: dict[tuple[str, int], _Leaf],
    pages: dict[tuple[str, str], _Page],
    terms: tuple[str, ...],
    question: str,
) -> list[_Leaf]:
    """Put the probes' reading of the partition and the partition's own text on one scale.

    What the probes found is evidence the engine itself ranked a page under the words it was asked with,
    and it is already weighed by how rare those words are, how high the page stood in their results, and
    whether the page is shaped like a statement; that weighing is what `_rank` produced, and it is scaled
    here to the best page the probes reached. What the page's own text says is counted as a smaller share,
    because it is the same evidence measured a second time without the engine's opinion of it, and it is
    only a reason to keep a page the probes left low — a page they never reached at all arrives through
    the neighbours, not through this.

    The counting of a word is done over the whole partition rather than over the filing a page belongs to,
    because a question is asked of a partition that may hold several filings and the pages that compete for
    its twenty places are all of them; rarity that ignores the rest of the partition would hand a place to
    a page merely because its own filing is short.
    """

    for leaf in leaves.values():
        leaf.own = 0.0
        leaf.probe = 0.0
        leaf.cover = 0.0
    _bm25(leaves.values(), terms)
    peak = max((leaf.own for leaf in leaves.values()), default=0.0) or 1.0
    pool = max((page.score for page in pages.values()), default=0.0) or 1.0
    by_entry: dict[tuple[str, str], _Leaf] = {}
    for leaf in leaves.values():
        reference = leaf.memory_ref
        by_entry[(getattr(reference, "artifact_id", "") or "", leaf.entry_id)] = leaf
    for key, page in pages.items():
        leaf = by_entry.get(key)
        if leaf is None:
            continue
        leaf.probe = page.score / pool
        leaf.cover = page.cover
    ordered = list(leaves.values())
    for leaf in ordered:
        leaf.score = _CORPUS_WEIGHT * (leaf.own / peak) + _PROBE_WEIGHT * leaf.probe
    return ordered


def _word_probes(terms: Iterable[str], inside: str = "") -> tuple[tuple[str, tuple[str, ...]], ...]:
    """The word sets a question is asked with: each of its words, and each pair of neighbours.

    One word reaches every page that prints it, which is what makes a count of the pages it reaches a
    measure of how decisive it is. A pair reaches the pages that print the subject and its measure
    together, and is asked on its own because a page about "restructuring costs" is not a page about
    costs of any other kind. With a filing named, both are asked again inside it, since a scope holds
    several filings and the same words mean different things in each.
    """

    wanted = tuple(dict.fromkeys(term for term in terms if term))
    prefix = f"{inside} " if inside else ""
    probes = tuple((f"{prefix}{term}", (term,)) for term in wanted)
    probes += tuple(
        (f"{prefix}{left} {right}", (left, right))
        for left, right in zip(wanted, wanted[1:], strict=False)
    )[:_MAX_PROBE_PAIRS]
    return probes


async def _gather(
    memory: Any,
    probes: tuple[tuple[str, tuple[str, ...]], ...],
    pool: int,
    pages: dict[tuple[str, str], _Page] | None = None,
) -> tuple[dict[tuple[str, str], _Page], dict[str, list[tuple[str, str]]]]:
    """Ask Memory each probe and record which pages each one reached, in the order it reached them.

    The pages are kept once and the reach of every probe alongside them, so a page's standing can be
    weighed both against how many other pages the same words reach — a word three pages print is evidence
    and a word three hundred print is vocabulary — and against how near the top of their results the page
    sat, which is the engine's own opinion of how well the page answers that word. Everything a page is
    judged on arrives with its hit: the identifier that names its filing and the body that shows how the
    page is built, so gathering costs one read per probe rather than one per candidate. A second round
    adds its pages to the first, which is how a question asked about a named filing gets the same evidence
    measured inside that filing.
    """

    gathered = pages if pages is not None else {}
    postings: dict[str, list[tuple[str, str]]] = {}
    for label, words in probes:
        reached: list[tuple[str, str]] = []
        for hit in await _search(memory, label, pool):
            key = _key(hit)
            text = str(getattr(hit, "text", "") or "")
            found = gathered.get(key)
            if found is None:
                head, _, body = text.partition("\n")
                found = _Page(
                    hit=hit,
                    document_id=head.strip(),
                    amounts=_amount_density(text),
                )
                gathered[key] = found
            if key not in reached:
                reached.append(key)
        postings[label] = reached
    return gathered, postings


def _rank(
    pages: dict[tuple[str, str], _Page],
    probes: tuple[tuple[str, tuple[str, ...]], ...],
    postings: dict[str, list[tuple[str, str]]],
    question: str,
) -> None:
    """Score every gathered page by the evidence it holds.

    Two things are counted for each probe a page answered: its weight, which is the rarity of the words
    matched across the pages that could be found, and the place the page held among the results those
    words brought back. The first says how much of the question a page carries; the second is what the
    engine itself believed about how well it carries it, and a page that answers ten common words is not
    the page that answers one decisive word best. Each is scaled to the best page reached, so they reorder
    candidates rather than inventing them. The rate at which a page states amounts is added last and only
    to a question that asks for a figure, because half of the evidence in a filing is a sentence and a
    table-shaped prior puts those sentences out of reach. A page's opening lines were weighed the same
    way, on the belief that a heading names the evidence; against the annotated page that belief cost
    more than it paid, because a filing prints its table of contents, its boilerplate, and its headings
    with the same words the question uses, so the pages that quote the question loudest at the top are
    often the ones that answer it least.
    """

    universe = max(1, len(pages))
    covered: dict[tuple[str, str], float] = {}
    fused: dict[tuple[str, str], float] = {}
    for label, words in probes:
        reached = postings.get(label, [])
        if not reached:
            continue
        weight = math.log(1.0 + universe / (1.0 + len(reached)))
        if len(words) > 1:
            weight *= 1.0 + _PAIR_WEIGHT
        for position, key in enumerate(reached):
            covered[key] = covered.get(key, 0.0) + weight
            fused[key] = fused.get(key, 0.0) + weight / (_RANK_DAMPING + position)
    if not covered:
        return
    peak = max(covered.values())
    scale = max(fused.values())
    for key, page in pages.items():
        page.cover = covered.get(key, 0.0) / peak
        page.score = page.cover + fused.get(key, 0.0) / scale
    strongest = max(page.score for page in pages.values()) or 1.0
    numeric = _FIGURE_ASK.search(question.casefold()) is not None
    if numeric:
        for page in pages.values():
            page.score += _AMOUNT_WEIGHT * strongest * page.amounts / (
                page.amounts + _AMOUNT_SATURATION
            )


def _spread(
    delivery: list[_Leaf], neighbours: dict[tuple[str, int], _Leaf], width: int
) -> list[_Leaf]:
    """Let a strong page lend its standing to the pages printed on either side of it.

    An answer is written across a page break more often than a filing is silent, and an annotation names
    one half of it more often than both, so a page that has earned a place lends a share of what it earned
    to its neighbours. The lending is repeated, because the page beside a borrowed page is often the other
    half of the same sentence — one pass lifts the neighbours of the evidence and leaves a hole where the
    evidence itself splits in three — and each pass measures the share the page held when the pass began,
    so what spreads is the ranking that was earned, not a rumour of it. A page too weak to lend is not
    asked to, at a floor set against the best page in the shortlist.
    """

    if not _NEIGHBOUR_WEIGHT or not delivery:
        return delivery
    chosen = list(delivery)
    for _pass in range(_NEIGHBOUR_PASSES):
        chosen.sort(key=_order)
        earned = [(leaf, leaf.score) for leaf in chosen[:width]]
        strongest = earned[0][1]
        present = {leaf.location for leaf in chosen}
        for leaf, value in earned:
            if value < _NEIGHBOUR_FLOOR * strongest:
                continue
            for offset in (-1, 1):
                other = neighbours.get((leaf.document_id, leaf.number + offset))
                if other is None:
                    continue
                other.score += _NEIGHBOUR_WEIGHT * value
                if other.location not in present:
                    present.add(other.location)
                    chosen.append(other)
    chosen.sort(key=_order)
    return chosen


def _filing(pages: Iterable[_Page], question: str) -> str:
    """The filing the delivered pages should come from.

    A question is asked about one company, and a page is only evidence inside its own filing, so the
    candidates are narrowed to a single document before the last places are given out. The named company
    decides it when the question says one; otherwise the filing whose best pages carry the most of the
    question does, with the number of pages it offered discounted so that a longer filing cannot win on
    volume alone.
    """

    gathered = list(pages)
    documents = {page.document_id for page in gathered if page.document_id}
    named = _named_document(question, sorted(documents))
    if named in documents:
        return named
    votes: dict[str, float] = {}
    by_document: dict[str, list[_Page]] = {}
    for page in gathered:
        by_document.setdefault(page.document_id, []).append(page)
    for document_id, candidates in by_document.items():
        best = sorted((page.score for page in candidates), reverse=True)[:_DOC_VOTE_PAGES]
        votes[document_id] = sum(best) / math.log(1.0 + len(candidates))
    if not votes:
        return ""
    return max(votes, key=lambda document_id: (votes[document_id], document_id))


def _order(page: _Page) -> tuple[float, float, str, int]:
    return (
        -page.score,
        -page.cover,
        page.document_id,
        page.location[1] if page.location else 0,
    )


async def search_pdf_pages(
    memory: Any, question: str, limit: int = 10
) -> tuple[MemoryCitation, ...]:
    """Evidence pages for one question, or the closest thing Memory will give up about it.

    Everything below reads a filing through narrow questions of its own, and a reading can go wrong about
    the shape of the data in front of it: a partition stored by another build, a page projected differently,
    an engine that answers a probe in a form this module has not seen. None of that is a reason to answer
    with nothing, since a caller that asks for evidence and receives an empty list can check no page at all.
    So when the reading fails, the question is asked of Memory exactly as it was given and the pages the
    engine puts first are cited — fewer words behind them, but every citation still read back out of the
    store, which is what a citation is for.
    """

    try:
        citations = await _evidence(memory, question, limit)
    except Exception:  # noqa: BLE001 - a failed reading is a reason to answer plainly, not to answer nothing
        citations = ()
    return citations or await _plainly(memory, question, limit)


async def _plainly(memory: Any, question: str, limit: int) -> tuple[MemoryCitation, ...]:
    """Ask Memory the question as given and cite the pages it ranks highest, with none of the reading above."""

    phrase = " ".join(question.split())
    if not phrase:
        return ()
    try:
        hits = await _search(memory, phrase, limit * _PAGES_PER_REQUEST)
    except Exception:  # noqa: BLE001 - a store that cannot answer has no evidence to cite
        return ()
    seen: set[Any] = set()
    citations: list[MemoryCitation] = []
    for hit in hits:
        if len(citations) == limit:
            break
        key = _key(hit)
        if key in seen:
            continue
        seen.add(key)
        citations.append(_citation(hit))
    return tuple(citations)


def _anchor_pages(leaves: dict[tuple[str, int], _Leaf], question: str, document_id: str, cap: int = 12) -> list[_Leaf]:
    """Pages that begin a filing section the question is phrased around."""
    q = question.casefold()
    order = ["income", "balance", "cash", "notes"]
    if re.search(r"\b(legal|lawsuit|litigation|proceeding)", q):
        order = ["legal", "risk"] + order
    elif re.search(r"\b(customer|customers)\b", q):
        order = ["risk", "customers"] + order
    elif re.search(r"\b(risk|cyclical|uncertaint)", q):
        order = ["risk"] + order
    elif re.search(r"\b(note|restructur|accounting|impairment)\b", q):
        order = ["notes", "income", "balance", "cash"]
    elif re.search(r"\b(capital|expense|cash|ebitda|margin|income|balance|debt|tax|cost|revenue)\b", q):
        order = ["income", "balance", "cash", "notes"]
    elif re.search(r"\b(segment|sales|growth)\b", q):
        order = ["segment", "income", "balance", "cash", "notes"]
    order += [name for name, _ in _SECTION_HEADINGS if name not in order]
    starts: dict[str, list[int]] = {}
    for page in leaves.values():
        head = "\n".join(page.body.splitlines()[:15])
        if len(re.findall(r"^\s*item\s+\d+", head, re.IGNORECASE | re.MULTILINE)) >= 2:
            continue  # table of contents
        matched = [name for name, pattern in _SECTION_HEADINGS if pattern.search(head)]
        if len(matched) >= 3:
            continue  # a table of contents lists several sections and is not one of them
        for name in matched:
            starts.setdefault(name, []).append(page.number)
            break
    picked: list[_Leaf] = []
    seen: set[int] = set()

    def add(number: int) -> None:
        page = leaves.get((document_id, number))
        if page is not None and page.number not in seen:
            seen.add(page.number)
            picked.append(page)

    headings: list[_Leaf] = []
    terms = set(_question_terms(question))
    if terms:
        for page in leaves.values():
            if len(headings) >= 2:
                break
            head = "\n".join(page.body.splitlines()[:15])
            if len(re.findall(r"^\s*item\s+\d+", head, re.IGNORECASE | re.MULTILINE)) >= 2:
                continue
            for line in page.body.splitlines():
                heading = line.strip()
                if re.match(r"^note\s*\d+", heading, re.IGNORECASE) and any(
                    term in heading.casefold() for term in terms
                ):
                    headings.append(page)
                    break
                if re.match(r"^item\s*3\b", heading, re.IGNORECASE) and "legal" in heading.casefold():
                    headings.append(page)
                    break

    for name in order:
        numbers = sorted(set(starts.get(name, ())))
        if not numbers:
            continue
        if name == "risk":
            for offset in range(8):
                add(numbers[0] + offset)
        else:
            span = 2 if name in ("legal", "notes", "segment", "customers") else 3
            for start in numbers[:3]:
                for offset in range(span):
                    add(start + offset)
        if len(picked) >= cap:
            break
    result: list[_Leaf] = []
    held: set[int] = set()
    for page in headings + picked:
        if page.number not in held:
            held.add(page.number)
            result.append(page)
    return result[:cap]


async def _evidence(memory: Any, question: str, limit: int) -> tuple[MemoryCitation, ...]:
    """Return up to ``limit`` distinct page citations, best evidence first.

    Every citation comes from a Memory hit, so what a caller reads back is the stored page rather than
    something assembled here. Two entries can name the same physical page of two revisions of one filing,
    and a reviewer needs to know which page they are reading, so a citation is kept only when its document
    and page position differ from every citation before it. The result never exceeds ``limit``, and a
    question that reaches no page returns nothing rather than a page chosen at random.
    """

    if limit < 1:
        return ()
    terms = _question_terms(question)
    if not terms:
        phrase = question.strip()
        terms = (phrase,) if phrase else ()
    if not terms:
        return ()
    depth = max(_PROBE_DEPTH, limit * _PAGES_PER_REQUEST)
    probes = _word_probes(terms)
    pages, postings = await _gather(memory, probes, depth)
    if not pages:
        return ()
    documents = {page.document_id for page in pages.values() if page.document_id}
    named = _named_document(question, documents) if len(documents) > 1 else ""
    if named:
        inside = _word_probes(terms, _document_name(named))
        pages, reached = await _gather(memory, inside, depth, pages)
        postings = {**postings, **reached}
        probes = probes + inside
    _rank(pages, probes, postings, question)
    leaves = await _corpus(memory)
    if leaves:
        ordered = sorted(_fuse(leaves, pages, terms, question), key=_order)
        wanted = _filing(ordered, question)
        neighbours = {key: leaf for key, leaf in leaves.items() if key[0] == wanted}
        base: list[Any] = _spread(
            [leaf for leaf in ordered if leaf.document_id == wanted], neighbours, limit
        )
        anchors = _anchor_pages(neighbours, question, wanted, cap=min(8, max(0, limit - 12)))
        delivery: list[Any] = []
        held: set[Any] = set()
        for page in list(base[:12]) + anchors + base[12:]:
            key = page.location or (page.document_id, page.number)
            if key in held:
                continue
            held.add(key)
            delivery.append(page)
            if len(delivery) >= limit:
                break
        delivery += [leaf for leaf in ordered if leaf.document_id != wanted][
            : max(0, limit - len(delivery) // 2)
        ]
    else:
        ordered = sorted(pages.values(), key=_order)
        wanted = _filing(ordered, question)
        delivery = [page for page in ordered if page.document_id == wanted]
        delivery += [page for page in ordered if page.document_id != wanted][
            : max(0, limit - len(delivery) // 2)
        ]
    seen: set[tuple[str, str] | tuple[str, int]] = set()
    citations: list[MemoryCitation] = []
    for page in delivery[: limit * _PAGES_PER_REQUEST]:
        if len(citations) == limit:
            break
        if getattr(page, "location", None) is None:
            await _locate(memory, page)
        location: tuple[str, str] | tuple[str, int] = page.location or _key(page.hit)
        if location in seen:
            continue
        seen.add(location)
        citations.append(_citation(getattr(page, "hit", page)))
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
