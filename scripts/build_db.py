import argparse
import json
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

try:
    from pdfminer.high_level import extract_text
    from pdfminer.pdfpage import PDFPage
except ImportError as exc:  # pragma: no cover - handled at runtime
    raise RuntimeError(
        "pdfminer.six is required to build the Oxford DB. Install it before running this script."
    ) from exc


REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCES_DIR = REPO_ROOT / "sources"
DATA_DIR = REPO_ROOT / "data"
DEBUG_DIR = DATA_DIR / "debug"

PDF_3000_PATH = SOURCES_DIR / "American_Oxford_3000.pdf"
PDF_5000_PATH = SOURCES_DIR / "American_Oxford_5000_by_CEFR_level.pdf"
DB_PATH = DATA_DIR / "oxford_words.db"
JSONL_PATH = DATA_DIR / "oxford_words.jsonl"

CEFR_LEVELS = {"A1", "A2", "B1", "B2", "C1"}
BASIC_POS = ["n", "v", "adj", "adv", "prep", "conj", "pron", "det", "exclam"]
ATOMIC_POS = [
    "modal v.",
    "auxiliary v.",
    "indefinite article",
    "definite article",
    "infinitive marker",
]
ALLOWED_POS = {
    "n.",
    "v.",
    "adj.",
    "adv.",
    "prep.",
    "conj.",
    "pron.",
    "det.",
    "number",
    "exclam.",
    "modal v.",
    "auxiliary v.",
    "indefinite article",
    "definite article",
    "infinitive marker",
    "adj./adv.",
    "det./pron.",
    "det./number",
    "adj./pron.",
    "exclam./n.",
    "conj./prep.",
    "pron./det.",
    "det./adj.",
    "conj./adv.",
    "adv./prep.",
    "prep./adv.",
    "det./pron./adv.",
    "number/det.",
}
SLASH_POS = {token for token in ALLOWED_POS if "/" in token}
HEADER_PATTERNS = [
    re.compile(r"^The Oxford \d+™"),
    re.compile(r"^© Oxford University Press"),
    re.compile(r"^\(American English\)$"),
    re.compile(r"^The Oxford \d+ is "),
    re.compile(r"^3000, it includes "),
    re.compile(r"^\d+ / \d+\s*$"),
    re.compile(r"^by CEFR level\s*$"),
]
CEFR_RE = re.compile(r"\b(A1|A2|B1|B2|C1)\b")
WORD_RE = re.compile(
    r"^(?P<word>[a-zA-Z][\w\s\-']*?(?:\s*\([^)]+\))?)"
    r"\s+(?=[a-z]+\.|modal\b|auxiliary\b|indefinite\b|definite\b|"
    r"infinitive\b|number\b)"
)
MULTI_WORD_PREFIX_RE = re.compile(
    r"^(?P<words>[a-zA-Z]+(?:,\s*[a-zA-Z]+)+)\s+"
    r"(?P<rest>(?:indefinite|definite)\s+article\s+[A-C][12])$"
)


@dataclass
class WordRow:
    word_raw: str
    base_word: str
    disambiguation: Optional[str]
    homonym_num: Optional[int]
    pos: str
    cefr: str
    source: str
    source_page: int
    source_order: int
    raw_line_ref: str


@dataclass
class BuildState:
    counters: Counter = field(default_factory=Counter)
    header_removals: list[str] = field(default_factory=list)
    multiline_merges: list[str] = field(default_factory=list)
    artifact_removals: list[str] = field(default_factory=list)
    rejects: list[str] = field(default_factory=list)
    homonym_changes: list[str] = field(default_factory=list)


def extract_pages(pdf_path: Path) -> list[tuple[int, list[str]]]:
    result: list[tuple[int, list[str]]] = []
    with pdf_path.open("rb") as handle:
        num_pages = sum(1 for _ in PDFPage.get_pages(handle))
    for page_index in range(num_pages):
        text = extract_text(str(pdf_path), page_numbers=[page_index])
        result.append((page_index + 1, text.split("\n")))
    return result


def is_header_line(line: str) -> bool:
    return any(pattern.search(line) for pattern in HEADER_PATTERNS)


def remove_headers(
    pages: list[tuple[int, list[str]]],
    *,
    source: str,
    state: BuildState,
) -> list[tuple[int, list[str]]]:
    cleaned_pages: list[tuple[int, list[str]]] = []
    for page_no, lines in pages:
        cleaned_lines: list[str] = []
        for line_no, raw_line in enumerate(lines, start=1):
            stripped = raw_line.strip()
            if stripped and is_header_line(stripped):
                cleaned_lines.append("")
                state.header_removals.append(f"{source}\tp{page_no}:l{line_no}\t{stripped}")
                state.counters[f"header_removed_{source}"] += 1
                continue
            cleaned_lines.append(raw_line)
        cleaned_pages.append((page_no, cleaned_lines))
    return cleaned_pages


def merge_multiline_3000(
    lines: list[str],
    *,
    page_no: int,
    state: BuildState,
) -> list[tuple[int, str]]:
    non_empty = [(line_no, line.rstrip()) for line_no, line in enumerate(lines, start=1) if line.strip()]
    merged: list[tuple[int, str]] = []
    skip_next = False
    for index, (line_no, current) in enumerate(non_empty):
        if skip_next:
            skip_next = False
            continue
        next_line = non_empty[index + 1][1] if index + 1 < len(non_empty) else None
        next_line_no = non_empty[index + 1][0] if index + 1 < len(non_empty) else None

        if next_line is not None and next_line.strip() in CEFR_LEVELS:
            merged_line = f"{current} {next_line.strip()}"
            merged.append((line_no, merged_line))
            state.multiline_merges.append(
                f"C\tp{page_no}:l{line_no}\tp{page_no}:l{next_line_no}\t{current}\t{next_line.strip()}\t{merged_line}"
            )
            state.counters["multiline_type_C"] += 1
            skip_next = True
            continue

        if current.rstrip().endswith((",", "/")) and next_line is not None:
            merged_line = f"{current} {next_line}"
            has_cefr = any(re.search(rf"\b{level}\b", current) for level in CEFR_LEVELS)
            pattern = "B" if has_cefr else "A"
            merged.append((line_no, merged_line))
            state.multiline_merges.append(
                f"{pattern}\tp{page_no}:l{line_no}\tp{page_no}:l{next_line_no}\t{current}\t{next_line}\t{merged_line}"
            )
            state.counters[f"multiline_type_{pattern}"] += 1
            skip_next = True
            continue

        merged.append((line_no, current))
    return merged


def _normalize_slash_pos(match: re.Match) -> str:
    pos_abbr = {
        "n",
        "v",
        "adj",
        "adv",
        "prep",
        "conj",
        "pron",
        "det",
        "exclam",
        "number",
    }
    left, right = match.group(1), match.group(2)
    if left not in pos_abbr or right not in pos_abbr:
        return match.group(0)
    left_token = left if left == "number" else f"{left}."
    right_token = right if right == "number" else f"{right}."
    return f"{left_token}/{right_token}"


def repair_pos_punctuation(line: str, *, enable_aggressive_repair: bool) -> tuple[str, int]:
    repairs = 0
    for pos in BASIC_POS:
        line, count = re.subn(rf"\b{pos},(?=\s|$)", f"{pos}.", line)
        repairs += count

    for pos in BASIC_POS:
        line, count = re.subn(rf"\b{pos}(?=\s+(A1|A2|B1|B2|C1)\b)", f"{pos}.", line)
        repairs += count

    if enable_aggressive_repair:
        aggressive_patterns = [
            (r"\bv\s+n\.", "v., n."),
            (r"\bn\s+v\.", "n., v."),
            (r"\badj\s+adv\.", "adj., adv."),
            (r"\badv\s+adj\.", "adv., adj."),
        ]
        for pattern, replacement in aggressive_patterns:
            line, count = re.subn(pattern, replacement, line)
            repairs += count
    return line, repairs


def normalize(
    line: str,
    *,
    state: BuildState,
    enable_aggressive_repair: bool,
) -> str:
    apostrophe_count = line.count("\u2019")
    if apostrophe_count:
        state.counters["apostrophe_replacements"] += apostrophe_count
        line = line.replace("\u2019", "'")

    line, compact_cefr_count = re.subn(r"(\.)([A-Z][0-9])", r"\1 \2", line)
    if compact_cefr_count:
        state.counters["compact_cefr_spacing_repairs"] += compact_cefr_count

    line, noun_count = re.subn(r"\bnoun\.", "n.", line)
    if noun_count:
        state.counters["noun_normalizations"] += noun_count

    line, pos_period_count = re.subn(
        r"\b(n|v|adj|adv|prep|conj|pron|det|exclam)(?=\s*,|\s*$)",
        r"\1.",
        line,
    )
    if pos_period_count:
        state.counters["pos_period_restorations"] += pos_period_count

    line, repair_count = repair_pos_punctuation(
        line,
        enable_aggressive_repair=enable_aggressive_repair,
    )
    if repair_count:
        state.counters["pos_punctuation_repairs"] += repair_count

    def replace_slash(match: re.Match) -> str:
        replacement = _normalize_slash_pos(match)
        if replacement != match.group(0):
            state.counters["slash_pos_whitespace_normalizations"] += 1
        return replacement

    line = re.sub(
        r"\b([a-z]+)\.?\s*/\s*([a-z]+)\.?(?=[\s,]|$)",
        replace_slash,
        line,
    )

    line = re.sub(r"\s+", " ", line).strip()
    return line


def expand_multi_word_line(line: str) -> list[str]:
    match = MULTI_WORD_PREFIX_RE.match(line)
    if not match:
        return [line]
    words = [word.strip() for word in match.group("words").split(",")]
    rest = match.group("rest")
    return [f"{word} {rest}" for word in words]


def split_word(word_raw: str) -> tuple[str, Optional[str], Optional[int]]:
    disambiguation = None
    match = re.search(r"\s*\(([^)]+)\)\s*", word_raw)
    base = word_raw
    if match:
        disambiguation = match.group(1).strip()
        base = (word_raw[: match.start()] + word_raw[match.end() :]).strip()

    homonym_num = None
    number_match = re.match(r"^(?P<base>.+?)(?P<num>[12])$", base)
    if number_match:
        base = number_match.group("base")
        homonym_num = int(number_match.group("num"))

    return base, disambiguation, homonym_num


def extract_word_part(line: str) -> tuple[str, str]:
    match = WORD_RE.match(line)
    if not match:
        raise ValueError(f"WORD_RE did not match: {line}")
    word_raw = match.group("word").strip()
    trailing_word = word_raw.split()[-1]
    if trailing_word in BASIC_POS:
        raise ValueError(f"WORD_RE consumed trailing POS fragment: {line}")
    rest = line[match.end() :].strip()
    if not rest:
        raise ValueError(f"POS payload missing: {line}")
    return word_raw, rest


def tokenize_pos_chunk(chunk: str) -> list[str]:
    tokens: list[str] = []
    for token in (part.strip() for part in chunk.split(",")):
        if not token:
            continue
        if token not in ALLOWED_POS:
            raise ValueError(f"Unrecognized POS token: {token}")
        tokens.append(token)
    if not tokens:
        raise ValueError(f"Empty POS chunk: {chunk}")
    return tokens


def tokenize_pos_cefr(rest: str) -> list[tuple[str, str]]:
    matches = list(CEFR_RE.finditer(rest))
    if not matches:
        raise ValueError(f"No CEFR token found: {rest}")

    pairs: list[tuple[str, str]] = []
    previous_end = 0
    for match in matches:
        chunk = rest[previous_end : match.start()].strip(" ,")
        cefr = match.group(1)
        if not chunk:
            raise ValueError(f"Missing POS before CEFR {cefr}: {rest}")
        for pos in tokenize_pos_chunk(chunk):
            pairs.append((pos, cefr))
        previous_end = match.end()

    trailing = rest[previous_end:].strip(" ,")
    if trailing:
        raise ValueError(f"Unexpected trailing text after CEFR scan: {trailing}")
    return pairs


def strip_5000_artifact(
    line: str,
    *,
    current_section: str,
    page_no: int,
    line_no: int,
    state: BuildState,
) -> str:
    match = re.search(r"\s+(B2|C1)$", line)
    if match and match.group(1) == current_section:
        cleaned = line[: match.start()]
        state.artifact_removals.append(
            f"p{page_no}:l{line_no}\t{line}\t{cleaned}\tsection={current_section}"
        )
        state.counters["artifact_removals"] += 1
        return cleaned
    return line


def reject_line(
    *,
    state: BuildState,
    source: str,
    page_no: int,
    line_no: int,
    line: str,
    reason: str,
) -> None:
    state.rejects.append(f"{source}\tp{page_no}:l{line_no}\t{reason}\t{line}")
    state.counters["rejects"] += 1


def parse_3000(
    pages: list[tuple[int, list[str]]],
    *,
    state: BuildState,
    enable_aggressive_repair: bool,
) -> list[WordRow]:
    rows: list[WordRow] = []
    source_order = 1
    for page_no, lines in pages:
        merged_lines = merge_multiline_3000(lines, page_no=page_no, state=state)
        for line_no, raw_line in merged_lines:
            normalized = normalize(
                raw_line,
                state=state,
                enable_aggressive_repair=enable_aggressive_repair,
            )
            if not normalized:
                continue
            expanded_lines = expand_multi_word_line(normalized)
            if expanded_lines != [normalized]:
                state.counters["multi_word_prefix_expansions"] += 1
            for expanded_line in expanded_lines:
                try:
                    word_raw, rest = extract_word_part(expanded_line)
                    pos_pairs = tokenize_pos_cefr(rest)
                except ValueError as exc:
                    reject_line(
                        state=state,
                        source="Oxford 3000",
                        page_no=page_no,
                        line_no=line_no,
                        line=expanded_line,
                        reason=str(exc),
                    )
                    continue
                base_word, disambiguation, homonym_num = split_word(word_raw)
                for pos, cefr in pos_pairs:
                    rows.append(
                        WordRow(
                            word_raw=word_raw,
                            base_word=base_word,
                            disambiguation=disambiguation,
                            homonym_num=homonym_num,
                            pos=pos,
                            cefr=cefr,
                            source="Oxford 3000",
                            source_page=page_no,
                            source_order=source_order,
                            raw_line_ref=f"p{page_no}:l{line_no}",
                        )
                    )
                    source_order += 1
    return rows


def parse_5000(
    pages: list[tuple[int, list[str]]],
    *,
    state: BuildState,
    enable_aggressive_repair: bool,
) -> list[WordRow]:
    rows: list[WordRow] = []
    source_order = 1
    current_level: Optional[str] = None
    for page_no, lines in pages:
        for line_no, raw_line in enumerate(lines, start=1):
            stripped = raw_line.strip()
            if not stripped:
                continue
            if stripped in {"B2", "C1"}:
                current_level = stripped
                continue
            if current_level is None:
                reject_line(
                    state=state,
                    source="Oxford 5000",
                    page_no=page_no,
                    line_no=line_no,
                    line=stripped,
                    reason="Encountered content before section header",
                )
                continue

            stripped = strip_5000_artifact(
                stripped,
                current_section=current_level,
                page_no=page_no,
                line_no=line_no,
                state=state,
            )
            normalized = normalize(
                stripped,
                state=state,
                enable_aggressive_repair=enable_aggressive_repair,
            )
            if not normalized:
                continue
            try:
                word_raw, rest = extract_word_part(normalized)
                pos_tokens = tokenize_pos_chunk(rest)
            except ValueError as exc:
                reject_line(
                    state=state,
                    source="Oxford 5000",
                    page_no=page_no,
                    line_no=line_no,
                    line=normalized,
                    reason=str(exc),
                )
                continue
            base_word, disambiguation, homonym_num = split_word(word_raw)
            for pos in pos_tokens:
                rows.append(
                    WordRow(
                        word_raw=word_raw,
                        base_word=base_word,
                        disambiguation=disambiguation,
                        homonym_num=homonym_num,
                        pos=pos,
                        cefr=current_level,
                        source="Oxford 5000",
                        source_page=page_no,
                        source_order=source_order,
                        raw_line_ref=f"p{page_no}:l{line_no}",
                    )
                )
                source_order += 1
    return rows


def postprocess_homonyms(rows: list[WordRow], *, state: BuildState) -> list[WordRow]:
    pairs: dict[str, set[int]] = {}
    for row in rows:
        if row.homonym_num is not None:
            pairs.setdefault(row.base_word, set()).add(row.homonym_num)

    affected_bases: set[str] = set()
    for row in rows:
        if row.homonym_num is None:
            continue
        if len(pairs.get(row.base_word, set())) < 2:
            state.homonym_changes.append(
                f"{row.base_word}\t{row.word_raw}\t{row.homonym_num}\t{row.raw_line_ref}\tNULL"
            )
            affected_bases.add(row.base_word)
            row.homonym_num = None
            state.counters["homonym_nullified_rows"] += 1
    state.counters["homonym_nullifications"] = len(affected_bases)
    return rows


def ensure_output_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)


def write_debug_logs(state: BuildState) -> None:
    log_map = {
        DEBUG_DIR / "multiline_merges.log": state.multiline_merges,
        DEBUG_DIR / "artifact_removals.log": state.artifact_removals,
        DEBUG_DIR / "header_removals.log": state.header_removals,
        DEBUG_DIR / "rejects.log": state.rejects,
    }
    for path, lines in log_map.items():
        path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def recreate_database(rows: list[WordRow]) -> None:
    if DB_PATH.exists():
        DB_PATH.unlink()

    with sqlite3.connect(DB_PATH) as conn:
        conn.executescript(
            """
            CREATE TABLE words (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                word_raw       TEXT NOT NULL,
                base_word      TEXT NOT NULL,
                disambiguation TEXT,
                homonym_num    INTEGER,
                pos            TEXT NOT NULL,
                cefr           TEXT NOT NULL,
                source         TEXT NOT NULL,
                source_page    INTEGER,
                source_order   INTEGER,
                raw_line_ref   TEXT
            );

            CREATE UNIQUE INDEX idx_word_pos_cefr
                ON words(base_word, COALESCE(disambiguation,''), COALESCE(homonym_num,0), pos, cefr, source);

            CREATE INDEX idx_base_word    ON words(base_word);
            CREATE INDEX idx_cefr         ON words(cefr);
            CREATE INDEX idx_pos          ON words(pos);
            CREATE INDEX idx_source       ON words(source);
            CREATE INDEX idx_source_order ON words(source, source_order);
            """
        )
        conn.executemany(
            """
            INSERT INTO words (
                word_raw,
                base_word,
                disambiguation,
                homonym_num,
                pos,
                cefr,
                source,
                source_page,
                source_order,
                raw_line_ref
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    row.word_raw,
                    row.base_word,
                    row.disambiguation,
                    row.homonym_num,
                    row.pos,
                    row.cefr,
                    row.source,
                    row.source_page,
                    row.source_order,
                    row.raw_line_ref,
                )
                for row in rows
            ],
        )
        conn.commit()


def write_jsonl() -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT
                id,
                word_raw,
                base_word,
                disambiguation,
                homonym_num,
                pos,
                cefr,
                source,
                source_page,
                source_order,
                raw_line_ref
            FROM words
            ORDER BY id
            """
        ).fetchall()
    with JSONL_PATH.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(dict(row), ensure_ascii=False) + "\n")


def can_parse_payload(line: str) -> bool:
    try:
        _, rest = extract_word_part(line)
        if CEFR_RE.search(rest):
            tokenize_pos_cefr(rest)
        else:
            tokenize_pos_chunk(rest)
    except ValueError:
        return False
    return True


def run_regression_tests() -> None:
    cases = [
        ("part-time adj./adv. B2", True, True, False),
        ("full-time adj./adv. B2", True, True, False),
        ("résumé n.", True, True, False),
        ("absorb v B2", False, True, False),
        ("alien n B2", False, True, False),
        ("dare v B2", False, True, False),
        ("light adj, A2", False, True, False),
        ("chat v n. A2", False, True, True),
    ]

    for line, expect_before, expect_after, aggressive in cases:
        before_ok = can_parse_payload(line)
        normalized = normalize(
            line,
            state=BuildState(),
            enable_aggressive_repair=aggressive,
        )
        after_ok = can_parse_payload(normalized)
        if before_ok != expect_before:
            raise AssertionError(f"Regression mismatch before normalize: {line}")
        if after_ok != expect_after:
            raise AssertionError(f"Regression mismatch after normalize: {line} -> {normalized}")
    print("✅ v2 §10.6 regression tests passed (8 cases)")


def grouped_rows(rows: list[WordRow]) -> dict[tuple, list[WordRow]]:
    groups: dict[tuple, list[WordRow]] = defaultdict(list)
    for row in rows:
        key = (row.base_word, row.disambiguation, row.homonym_num, row.source)
        groups[key].append(row)
    return groups


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def validate_rows(rows: list[WordRow], *, state: BuildState) -> None:
    groups = grouped_rows(rows)

    def find(
        *,
        base_word: Optional[str] = None,
        disambiguation: Optional[str] = None,
        homonym_num: Optional[int] = None,
        pos: Optional[str] = None,
        cefr: Optional[str] = None,
        source: Optional[str] = None,
        word_raw: Optional[str] = None,
    ) -> list[WordRow]:
        matched: list[WordRow] = []
        for row in rows:
            if base_word is not None and row.base_word != base_word:
                continue
            if disambiguation is not None and row.disambiguation != disambiguation:
                continue
            if homonym_num is not None and row.homonym_num != homonym_num:
                continue
            if pos is not None and row.pos != pos:
                continue
            if cefr is not None and row.cefr != cefr:
                continue
            if source is not None and row.source != source:
                continue
            if word_raw is not None and row.word_raw != word_raw:
                continue
            matched.append(row)
        return matched

    checks: list[tuple[str, bool]] = []

    checks.append(
        ("march n. C1 (Oxford 5000)", bool(find(base_word="march", pos="n.", cefr="C1", source="Oxford 5000")))
    )
    checks.append(
        ("march v. C1 (Oxford 5000)", bool(find(base_word="march", pos="v.", cefr="C1", source="Oxford 5000")))
    )
    checks.append(
        ("ring homonym 1 -> n. A2", bool(find(base_word="ring", homonym_num=1, pos="n.", cefr="A2")))
    )
    checks.append(
        (
            "ring homonym 2 -> v. A2 + n. B1",
            bool(find(base_word="ring", homonym_num=2, pos="v.", cefr="A2"))
            and bool(find(base_word="ring", homonym_num=2, pos="n.", cefr="B1")),
        )
    )
    checks.append(
        (
            "tear homonym 1 -> v. B2 + n. B2",
            bool(find(base_word="tear", homonym_num=1, pos="v.", cefr="B2"))
            and bool(find(base_word="tear", homonym_num=1, pos="n.", cefr="B2")),
        )
    )
    checks.append(
        ("tear homonym 2 -> n. B2", bool(find(base_word="tear", homonym_num=2, pos="n.", cefr="B2")))
    )
    checks.append(
        (
            "address -> n. A1 + v. B2",
            bool(find(base_word="address", pos="n.", cefr="A1"))
            and bool(find(base_word="address", pos="v.", cefr="B2")),
        )
    )
    checks.append(("a -> indefinite article A1", bool(find(base_word="a", pos="indefinite article", cefr="A1"))))
    checks.append(("an -> indefinite article A1", bool(find(base_word="an", pos="indefinite article", cefr="A1"))))
    checks.append(
        (
            "light (from the sun/a lamp) -> n. A1, adj. A1, v. A2",
            bool(find(base_word="light", disambiguation="from the sun/a lamp", pos="n.", cefr="A1"))
            and bool(find(base_word="light", disambiguation="from the sun/a lamp", pos="adj.", cefr="A1"))
            and bool(find(base_word="light", disambiguation="from the sun/a lamp", pos="v.", cefr="A2")),
        )
    )
    checks.append(
        ("light (not heavy) -> adj. A2", bool(find(base_word="light", disambiguation="not heavy", pos="adj.", cefr="A2")))
    )
    checks.append(
        (
            "double -> adj./det./pron./v. A2 + adv. B1",
            all(
                find(base_word="double", pos=pos, cefr="A2")
                for pos in ("adj.", "det.", "pron.", "v.")
            )
            and bool(find(base_word="double", pos="adv.", cefr="B1")),
        )
    )
    checks.append(
        (
            "minute boundary homonyms survive",
            bool(find(base_word="minute", homonym_num=1, pos="n.", cefr="A1", source="Oxford 3000"))
            and bool(find(base_word="minute", homonym_num=2, pos="adj.", cefr="C1", source="Oxford 5000")),
        )
    )
    checks.append(
        (
            "content boundary homonyms survive",
            bool(find(base_word="content", homonym_num=1, pos="n.", cefr="B1", source="Oxford 3000"))
            and bool(find(base_word="content", homonym_num=2, pos="adj.", cefr="C1", source="Oxford 5000")),
        )
    )
    last_groups = [
        key for key in groups if key[0] == "last" and key[2] is None and key[1] is not None
    ]
    checks.append(("last groups keep disambiguation with NULL homonym", len(last_groups) >= 2))
    checks.append(
        (
            "percent -> n. A2 + adj./adv. A2",
            bool(find(base_word="percent", pos="n.", cefr="A2"))
            and bool(find(base_word="percent", pos="adj./adv.", cefr="A2")),
        )
    )
    checks.append(("castle -> n. B2", bool(find(base_word="castle", pos="n.", cefr="B2", source="Oxford 5000"))))
    checks.append(
        (
            "teen -> n. B2 + adj. B2",
            bool(find(base_word="teen", pos="n.", cefr="B2"))
            and bool(find(base_word="teen", pos="adj.", cefr="B2")),
        )
    )
    checks.append(("o'clock -> adv. A1", bool(find(base_word="o'clock", pos="adv.", cefr="A1"))))
    checks.append(
        (
            "part-time / full-time -> adj./adv. B2",
            bool(find(base_word="part-time", pos="adj./adv.", cefr="B2"))
            and bool(find(base_word="full-time", pos="adj./adv.", cefr="B2")),
        )
    )
    checks.append(
        (
            "second1 -> det./number A1 + adv. A2",
            bool(
                find(
                    word_raw="second1 (next after the first)",
                    pos="det./number",
                    cefr="A1",
                )
            )
            and bool(
                find(
                    word_raw="second1 (next after the first)",
                    pos="adv.",
                    cefr="A2",
                )
            ),
        )
    )
    checks.append(("each -> det./pron./adv. A1", bool(find(base_word="each", pos="det./pron./adv.", cefr="A1"))))
    checks.append(
        (
            "one -> number/det. A1 + pron. A1",
            bool(find(base_word="one", pos="number/det.", cefr="A1"))
            and bool(find(base_word="one", pos="pron.", cefr="A1")),
        )
    )
    checks.append(("résumé -> n. B2", bool(find(base_word="résumé", pos="n.", cefr="B2", source="Oxford 5000"))))

    actual_slash_pos = {row.pos for row in rows if "/" in row.pos}
    checks.append(("all 13 slash POS tokens appear", actual_slash_pos == SLASH_POS))
    checks.append(("reject log is empty", state.counters["rejects"] == 0))

    for description, passed in checks:
        require(passed, f"Validation failed: {description}")
        print(f"✅ {description}")

    require(state.counters["multiline_type_A"] == 3, "Expected 3 type A multiline merges")
    require(state.counters["multiline_type_B"] == 2, "Expected 2 type B multiline merges")
    require(state.counters["multiline_type_C"] == 1, "Expected 1 type C multiline merge")
    require(state.counters["artifact_removals"] == 2, "Expected 2 Oxford 5000 artifact removals")
    require(state.counters["header_removed_Oxford 3000"] == 34, "Expected 34 Oxford 3000 header removals")
    require(state.counters["header_removed_Oxford 5000"] == 27, "Expected 27 Oxford 5000 header removals")
    require(state.counters["homonym_nullifications"] == 12, "Expected 12 homonym nullifications")
    require(state.counters["pos_period_restorations"] == 1, "Expected 1 POS period restoration")
    require(state.counters["apostrophe_replacements"] == 1, "Expected 1 apostrophe replacement")
    require(state.counters["slash_pos_whitespace_normalizations"] == 1, "Expected 1 slash POS whitespace normalization")
    require(state.counters["multi_word_prefix_expansions"] == 1, "Expected 1 multi-word prefix expansion")

    non_ascii_rows = sum(1 for row in rows if any(ord(char) > 127 for char in row.word_raw))
    require(non_ascii_rows == 1, f"Expected 1 non-ASCII row, found {non_ascii_rows}")

    jsonl_text = JSONL_PATH.read_text(encoding="utf-8")
    require('"résumé"' in jsonl_text, 'Expected literal "résumé" in JSONL output')
    require("\\u00e9" not in jsonl_text, "Expected JSONL to avoid escaped résumé characters")


def print_stats(rows: list[WordRow], *, state: BuildState) -> None:
    print("")
    print("Statistics")

    cefr_counts = Counter(row.cefr for row in rows)
    source_counts = Counter(row.source for row in rows)
    source_cefr_counts = Counter((row.source, row.cefr) for row in rows)
    pos_tokens = sorted({row.pos for row in rows})
    slash_pos_found = sorted({row.pos for row in rows if "/" in row.pos})
    boundary_homonyms = {
        row.base_word
        for row in rows
        if row.homonym_num is not None
    }

    print(f"- Total rows: {len(rows)}")
    print("- CEFR counts: " + ", ".join(f"{cefr}={cefr_counts[cefr]}" for cefr in sorted(CEFR_LEVELS)))
    print(
        "- Source counts: "
        + ", ".join(f"{source}={source_counts[source]}" for source in ("Oxford 3000", "Oxford 5000"))
    )
    print(
        "- Source x CEFR: "
        + ", ".join(
            f"{source}/{cefr}={source_cefr_counts[(source, cefr)]}"
            for source in ("Oxford 3000", "Oxford 5000")
            for cefr in sorted(CEFR_LEVELS)
            if source_cefr_counts[(source, cefr)]
        )
    )
    print("- POS tokens: " + ", ".join(pos_tokens))
    print(
        "- Multiline merges: "
        f"A={state.counters['multiline_type_A']}, "
        f"B={state.counters['multiline_type_B']}, "
        f"C={state.counters['multiline_type_C']}"
    )
    print(f"- Oxford 5000 artifact removals: {state.counters['artifact_removals']}")
    print(
        "- Header removals: "
        f"Oxford 3000={state.counters['header_removed_Oxford 3000']}, "
        f"Oxford 5000={state.counters['header_removed_Oxford 5000']}"
    )
    print(f"- Homonym nullifications: {state.counters['homonym_nullifications']}")
    print(f"- File-boundary homonym bases preserved: {', '.join(sorted({'content', 'minute'} & boundary_homonyms))}")
    print(f"- POS period restorations: {state.counters['pos_period_restorations']}")
    print(f"- POS punctuation fallback repairs: {state.counters['pos_punctuation_repairs']}")
    print(f"- Apostrophe replacements: {state.counters['apostrophe_replacements']}")
    print(f"- Slash POS whitespace normalizations: {state.counters['slash_pos_whitespace_normalizations']}")
    print(f"- Multi-word prefix expansions: {state.counters['multi_word_prefix_expansions']}")
    print(
        "- Non-ASCII word rows: "
        + str(sum(1 for row in rows if any(ord(char) > 127 for char in row.word_raw)))
    )
    print(f"- Reject count: {state.counters['rejects']}")
    print("- Slash POS observed: " + ", ".join(slash_pos_found))


def build(*, enable_aggressive_repair: bool) -> list[WordRow]:
    ensure_output_dirs()
    state = BuildState()

    pages_3000 = remove_headers(
        extract_pages(PDF_3000_PATH),
        source="Oxford 3000",
        state=state,
    )
    pages_5000 = remove_headers(
        extract_pages(PDF_5000_PATH),
        source="Oxford 5000",
        state=state,
    )

    rows_3000 = parse_3000(
        pages_3000,
        state=state,
        enable_aggressive_repair=enable_aggressive_repair,
    )
    rows_5000 = parse_5000(
        pages_5000,
        state=state,
        enable_aggressive_repair=enable_aggressive_repair,
    )

    rows = postprocess_homonyms(rows_3000 + rows_5000, state=state)
    recreate_database(rows)
    write_jsonl()
    write_debug_logs(state)
    run_regression_tests()
    validate_rows(rows, state=state)
    print_stats(rows, state=state)
    print("")
    print("Build successful")
    return rows


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--aggressive-pos-repair",
        action="store_true",
        help="Enable opt-in repair for whitespace-only POS chains such as 'v n.'.",
    )
    args = parser.parse_args()

    try:
        build(enable_aggressive_repair=args.aggressive_pos_repair)
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
