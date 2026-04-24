# Oxford PDF → SQLite + JSONL DB 생성 최종 가이드 (v4)

> **사용 맥락**: 이 문서는 Claude Code에서 `build_db.py` 스크립트를 작성·실행하기 위한 단일 진실 원본(SoT)이다.
> 4개 선행 문서(`radiant-splashing-stonebraker.md`, `additional_issues.md`, `radiant-splashing-stonebraker_v2.md`, `oxford_db_final_guide_v3.md`)와 PDF 원본 실측을 통합한 최종판.
>
> **v4 변경 요약**:
> - 본문 (§3~§10): v3 출시 후 발견된 결함 3건(P17·P18·P19) + 명시 사항 1건(v4-A) 반영
> - §11: 외부 LLM 분석 4건이 모두 PDF 표시 텍스트와 pdfminer 추출 결과의 차이로 인한 오인이었음을 검증
> - §12 (신규 부록): v3가 통째로 제거한 v2 §10(280줄)의 정책 5건을 체계적으로 복원 (방어적 fallback + 회귀 테스트 + OCR 범위 밖 정책)

---

## 1. 목적

Oxford 3000·5000 PDF 2개를 파싱하여 **통합 단어 DB**를 생성한다.

**산출물**:
```
vocabulary/
├── data/
│   ├── oxford_words.db       ← SQLite (Claude Code 스킬용 SQL 조회)
│   ├── oxford_words.jsonl    ← JSONL (로컬 LLM 컨텍스트 주입용)
│   └── debug/
│       ├── multiline_merges.log    ← 멀티라인 병합 기록
│       ├── artifact_removals.log   ← 5000 artifact 제거 기록
│       ├── header_removals.log     ← 헤더/푸터 제거 기록
│       └── rejects.log             ← 파싱 실패 라인
└── scripts/
    └── build_db.py
```

**입력**:
- `American_Oxford_3000.pdf` (A1~B2, ~3,000단어)
- `American_Oxford_5000_by_CEFR_level.pdf` (B2·C1 추가분, ~2,035단어)

**예상 최종 행 수**: ~5,600행 (다중 POS 전개 포함, 참고값일 뿐 품질 기준 아님)

---

## 2. PDF 포맷 핵심

### 3000 PDF — 라인 단위 self-contained
```
address n. A1, v. B2              ← POS마다 CEFR 다름
after prep. A1, conj., adv. A2    ← 같은 레벨이어도 여러 POS
ring1 n. A2                       ← homonym 숫자
ring2 v. A2, n.B1                 ← 공백 없는 경우 존재
light (not heavy) adj. A2         ← disambiguation (괄호)
a, an indefinite article A1       ← 멀티 단어 (쉼마 구분)
each det./pron./adv. A1           ← ⚠ 슬래시 3중 POS (v4 신규)
one number/det., pron. A1         ← ⚠ number이 좌측에 오는 슬래시 POS (v4 신규)
```

### 5000 PDF — 섹션 헤더로 레벨 결정
```
B2                      ← 섹션 헤더 (단독 라인)
absorb v.
bid n., v.
castle n. B2            ← ⚠ 줄 끝 B2 artifact (실측 2건)
résumé n.               ← ⚠ 비-ASCII 문자 é (U+00E9) 포함 (v4 명시)

C1
abolish v.
march n., v.
```

---

## 3. 실행 단계 (Step-by-step)

### Step 0. 의존성 및 디렉토리 준비

```python
# 의존성: pdfminer.six (이미 설치되어 있다고 가정)
from pdfminer.high_level import extract_text
from pdfminer.pdfpage import PDFPage
import sqlite3, json, re
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional
```

### Step 1. PDF → 원시 라인 추출 (페이지 단위)

페이지별로 추출하여 `source_page` 추적을 가능하게 한다.

```python
def extract_pages(pdf_path: str) -> list[tuple[int, list[str]]]:
    """페이지 번호(1-based)와 해당 페이지의 라인 리스트를 반환"""
    result = []
    with open(pdf_path, 'rb') as f:
        n_pages = sum(1 for _ in PDFPage.get_pages(f))
    for p in range(n_pages):
        txt = extract_text(pdf_path, page_numbers=[p])
        lines = txt.split('\n')
        result.append((p + 1, lines))
    return result
```

### Step 2. 헤더/푸터 제거

**제거 대상 패턴** (정규식):
- `^The Oxford \d+™`
- `^© Oxford University Press`
- `^\(American English\)`
- `^The Oxford \d+ is `
- `^3000, it includes `
- `^\d+ / \d+\s*$` (페이지 번호)
- `^by CEFR level\s*$`

빈 라인은 **유지**한다 (멀티라인 탐지에 필요).

debug 로그: 제거한 라인과 페이지 번호를 `header_removals.log`에 기록.

### Step 3. 멀티라인 병합 (3000 PDF 전용)

⚠ **이 단계는 반드시 3000 PDF에만 적용한다**. 5000 PDF는 섹션 헤더(`B2`, `C1`)를 단독 라인으로 쓰므로, 병합 로직이 오히려 오류를 만든다.

3가지 패턴이 존재한다. **타입 C를 가장 먼저 적용**해야 한다 (타입 A/B 조건과 겹치지 않기 위해).

#### 타입 C — 다음 줄이 단독 CEFR 토큰 (실측 1건)
```
line N  : 'double adj., det., pron., v. A2, adv.'   ← POS로 끝남
line N+1: ''
line N+2: 'B1'                                        ← 단독 CEFR
```
탐지: 다음 비어있지 않은 라인의 `.strip()`이 `{A1, A2, B1, B2, C1}` 중 하나.

#### 타입 A — CEFR 없이 쉼마/슬래시로 끝남 (실측 3건)
```
line N  : 'light (from the sun/a lamp) n.,'   ← CEFR 없음, 쉼마로 끝
line N+2: 'adj. A1, v. A2'
```
탐지: 현재 라인에 CEFR 토큰이 없고, trailing `,` 또는 `/`로 끝남.

#### 타입 B — trailing comma + 다음 줄 POS (실측 2건)
```
line N  : 'like (find sb/sth pleasant) v. A1,'   ← CEFR 있지만 쉼마로 끝
line N+2: 'n. B1'
```
탐지: 현재 라인에 CEFR이 있으나 `,`로 끝남.

#### 실측 총 6건 (병합 대상 전수 목록)

| 패턴 | 단어 | 원본 끝 | 연속 | 병합 결과 |
|------|------|---------|------|-----------|
| C | `double` | `adv.` | `B1` | `double adj., det., pron., v. A2, adv. B1` |
| A | `light (from the sun/a lamp)` | `n.,` | `adj. A1, v. A2` | `light (from the sun/a lamp) n., adj. A1, v. A2` |
| A | `match (contest/correspond)` | `n.,` | `v. A1` | `match (contest/correspond) n., v. A1` |
| A | `second1 (next after the first)` | `det./` | `number A1, adv. A2` | `second1 (next after the first) det./ number A1, adv. A2` |
| B | `like (find sb/sth pleasant)` | `v. A1,` | `n. B1` | `like (find sb/sth pleasant) v. A1, n. B1` |
| B | `outside` | `prep., noun.,` | `adj. A2` | `outside adv. A1, prep., noun., adj. A2` |

⚠ **주의 (v4 신규)**: 타입 A의 `second1` 케이스는 병합 결과가 `det./ number`처럼 슬래시 직후 공백이 1개 들어간다. 이 공백은 **Step 5의 슬래시 POS 정규화**에서 제거되어야 한다 (P17 신규).

구현 스켈레톤:
```python
CEFR_LEVELS = {"A1", "A2", "B1", "B2", "C1"}

def merge_multiline_3000(lines: list[str], log: list) -> list[tuple[int, str]]:
    """lines: 원본 라인 리스트 (빈 줄 포함), log: 병합 기록
    반환: [(원본 라인 번호, 병합된 라인)]"""
    non_empty = [(i, l.rstrip()) for i, l in enumerate(lines) if l.strip()]
    merged = []
    skip_next = False
    for idx, (lineno, cur) in enumerate(non_empty):
        if skip_next:
            skip_next = False
            continue
        nxt = non_empty[idx + 1][1] if idx + 1 < len(non_empty) else None

        # 타입 C 우선
        if nxt is not None and nxt.strip() in CEFR_LEVELS:
            merged_line = f"{cur} {nxt.strip()}"
            merged.append((lineno, merged_line))
            log.append(("C", lineno, cur, nxt, merged_line))
            skip_next = True
            continue

        # 타입 A/B (쉼마 또는 슬래시로 끝남)
        if cur.rstrip().endswith((",", "/")) and nxt is not None:
            merged_line = f"{cur} {nxt}"
            pattern = "A" if not any(re.search(rf"\b{lv}\b", cur) for lv in CEFR_LEVELS) else "B"
            merged.append((lineno, merged_line))
            log.append((pattern, lineno, cur, nxt, merged_line))
            skip_next = True
            continue

        merged.append((lineno, cur))
    return merged
```

### Step 4. 5000 PDF artifact 제거

5000 PDF에 한정: 줄 끝의 `B2` 또는 `C1`이 **현재 섹션 레벨과 동일**하면 제거.

**실측 2건**: `castle n. B2` (B2 섹션) / `delighted adj. B2` (B2 섹션).
이는 칼럼 레이아웃으로 인한 pdfminer 추출 artifact.

**v4 추가 검증**: 두 PDF 전수 스캔 결과 `B2`/`C1` 외 다른 trailing 알파벳 토큰 artifact는 없음. 이 패턴 2개로 충분.

```python
def strip_5000_artifact(line: str, current_section: str, log: list, lineno: int) -> str:
    m = re.search(r"\s+(B2|C1)$", line)
    if m and m.group(1) == current_section:
        cleaned = line[:m.start()]
        log.append((lineno, line, cleaned, current_section))
        return cleaned
    return line
```

### Step 5. 라인 정규화

**Step 3/4 이후** 적용 (trailing whitespace로 패턴 탐지하므로 순서 중요):

```python
def normalize(line: str) -> str:
    # [v3] 스페셜 아포스트로피 → 일반 아포스트로피
    # 실측: 3000 PDF line 1867 'o'clock adv. A1' (U+2019)
    line = line.replace("\u2019", "'")

    line = re.sub(r"(\.)([A-Z][0-9])", r"\1 \2", line)  # 'adj.B1' → 'adj. B1'
    line = re.sub(r"\bnoun\.", "n.", line)               # 'noun.' → 'n.'

    # [v3] POS 약어 마침표 복원
    # 실측: 5000 PDF line 652 'teen n., adj' (adj 뒤 마침표 누락)
    # 패턴: POS 약어가 쉼마/줄끝 직전에 마침표 없이 나타남
    line = re.sub(
        r"\b(n|v|adj|adv|prep|conj|pron|det|exclam)(?=\s*,|\s*$)",
        r"\1.",
        line,
    )

    # [v4 신규] 슬래시 POS 토큰 주변 공백 정규화
    # 실측: 멀티라인 병합 후 'second1 ... det./ number A1' 형태로 슬래시 직후 공백 1개 잔존
    # 'det./ number' → 'det./number', 'adj. /adv.' → 'adj./adv.'
    # POS 토큰 사이의 슬래시(/)에 한정 — 단어 본체 슬래시는 영향 없음
    line = re.sub(
        r"\b([a-z]+)\.?\s*/\s*([a-z]+)\.?(?=[\s,]|$)",
        lambda m: _normalize_slash_pos(m),
        line,
    )

    line = re.sub(r"\s+", " ", line).strip()             # 과도한 공백 정규화
    return line


def _normalize_slash_pos(m: re.Match) -> str:
    """슬래시 POS 정규화 — 양쪽이 모두 알려진 POS 약어인 경우만 변환.
    오탐 방지: disambiguation의 'sun/a lamp' 같은 단어 슬래시는 건드리지 않음."""
    POS_ABBR = {"n", "v", "adj", "adv", "prep", "conj", "pron", "det",
                "exclam", "number"}
    left, right = m.group(1), m.group(2)
    if left not in POS_ABBR or right not in POS_ABBR:
        return m.group(0)  # 원본 유지
    # number는 마침표 없음, 나머지는 마침표 부착
    left_tok = left if left == "number" else f"{left}."
    right_tok = right if right == "number" else f"{right}."
    return f"{left_tok}/{right_tok}"
```

**v4 정규화 추가 사항 (실측 근거)**:
- 슬래시 POS 주변 공백: 멀티라인 병합 결과로 `second1 (next after the first) det./ number A1, adv. A2`가 생성되면, Step 7에서 `det./ number` (공백 포함)가 ALLOWED_POS 미등재로 reject 위험.
  미처리 시 `second1` 단어의 `det./number A1` 행 1건 손실.

**v3 정규화 추가 사항 (이미 반영, 재확인)**:
- 스페셜 아포스트로피 U+2019: 3000 PDF에 1건 (`o'clock adv. A1`).
- POS 마침표 누락: 5000 PDF에 1건 (`teen n., adj`).

**주의**: POS 마침표 복원 정규식과 슬래시 POS 정규화 모두 단어 본체에는 영향을 주지 않는다. POS 마침표 복원은 `\b(n|v|adj|...)` + lookahead `(?=\s*,|\s*$)`로 위치 한정. 슬래시 정규화는 양쪽이 모두 `POS_ABBR` 집합에 속할 때만 변환하므로 disambiguation의 `sun/a lamp` 같은 단어 슬래시는 영향 없음.

### Step 5.5. 다중 단어 전처리 분기 (a, an 패턴) — v3 도입

**문제**: WORD_RE는 `^[a-zA-Z][\w\s\-']*?` 로 시작해서 첫 쉼마에서 매칭이 멈춘다.
실측 결과 `a, an indefinite article A1` 라인을 WORD_RE가 파싱 실패함을 확인:
```
✗ FAIL: 'a, an indefinite article A1'
```

**해결**: WORD_RE 적용 **전에** `[단어1, 단어2, ... POS_PHRASE CEFR]` 형태를 전처리로 분기.

```python
# 멀티 단어 + multi-word POS 패턴
# 예: "a, an indefinite article A1"
MULTI_WORD_PREFIX_RE = re.compile(
    r"^(?P<words>[a-zA-Z]+(?:,\s*[a-zA-Z]+)+)\s+"
    r"(?P<rest>(?:indefinite|definite)\s+article\s+[A-C][12])"
)

def expand_multi_word_line(line: str) -> list[str]:
    """
    'a, an indefinite article A1'
    → ['a indefinite article A1', 'an indefinite article A1']

    매칭 안 되면 원본 그대로 [line] 반환.
    """
    m = MULTI_WORD_PREFIX_RE.match(line)
    if not m:
        return [line]
    words = [w.strip() for w in m.group("words").split(",")]
    rest = m.group("rest")
    return [f"{w} {rest}" for w in words]
```

**실측 적용 대상**: 현재 PDF 전체에서 단 1건 (`a, an indefinite article A1`).
다른 multi-word POS 패턴(`modal v.`, `auxiliary v.`)은 WORD_RE의 `modal\b` / `auxiliary\b` lookahead로 처리됨.

### Step 6. 단어 · disambiguation · homonym 분리

⚠ **WORD_RE는 multi-word POS 시작 키워드를 lookahead에 포함**해야 한다.
**단** `a, an indefinite article A1` 처럼 쉼마로 단어가 분리된 경우는 WORD_RE 단독으로 처리 불가능 → **반드시 Step 5.5의 `expand_multi_word_line()`를 먼저 적용**한다.

```python
WORD_RE = re.compile(
    r"^(?P<word>[a-zA-Z][\w\s\-']*?(?:\s*\([^)]+\))?)"
    r"\s+(?=[a-z]+\.|modal\b|auxiliary\b|indefinite\b|definite\b|"
    r"infinitive\b|number\b)"
)

def split_word(word_raw: str) -> tuple[str, Optional[str], Optional[int]]:
    """
    'ring1'                → ('ring', None, 1)
    'light (not heavy)'    → ('light', 'not heavy', None)
    'last1 (final)'        → ('last', 'final', 1)
    'have to'              → ('have to', None, None)
    'résumé'               → ('résumé', None, None)
    """
    disambiguation = None
    m = re.search(r"\s*\(([^)]+)\)\s*", word_raw)
    base = word_raw
    if m:
        disambiguation = m.group(1).strip()
        base = (word_raw[:m.start()] + word_raw[m.end():]).strip()

    homonym_num = None
    m2 = re.match(r"^(?P<base>.+?)(?P<num>[12])$", base)
    if m2:
        base = m2.group("base")
        homonym_num = int(m2.group("num"))

    return base, disambiguation, homonym_num
```

**v4 비-ASCII 단어 검증 (재확인)**:
- 5000 PDF 1건 (`résumé n.`, U+00E9 = é)는 v3 WORD_RE의 `[a-zA-Z]` 시작 + `\w` 본체로 정상 매칭됨 (`\w`는 Python에서 기본적으로 유니코드 단어 문자 매칭).
- WORD_RE 코드 변경 불필요.
- ⚠ **JSONL 저장 시 주의**: §11의 결함 C 참조 — `ensure_ascii=False` 필수.

### Step 7. POS · CEFR 토큰화

**원자적 POS 우선 토큰화** (쪼개면 안 되는 것들):
```python
ATOMIC_POS = [
    "modal v.",
    "auxiliary v.",
    "indefinite article",
    "definite article",
    "infinitive marker",
]

ALLOWED_POS = {
    # 단일 POS
    "n.", "v.", "adj.", "adv.", "prep.", "conj.", "pron.", "det.",
    "number", "exclam.",
    # multi-word POS
    "modal v.", "auxiliary v.", "indefinite article", "definite article",
    "infinitive marker",
    # 슬래시 조합 POS — 실측 13종 전수 (v4에서 2종 추가)
    "adj./adv.",          # 6건
    "det./pron.",         # 18건
    "det./number",        # 1건  (second1 — Step 5 슬래시 정규화 후 매칭)
    "adj./pron.",         # 2건
    "exclam./n.",         # 2건
    "conj./prep.",        # 2건
    "pron./det.",         # 2건
    "det./adj.",          # 1건
    "conj./adv.",         # 1건
    "adv./prep.",         # 1건
    "prep./adv.",         # 1건
    "det./pron./adv.",    # 1건  ← v4 신규 (each)
    "number/det.",        # 1건  ← v4 신규 (one — number이 좌측)
}
```

**v4 보강 근거 (실측)**: v3 ALLOWED_POS에는 슬래시 POS 11종만 등재되어 있었으나, 두 PDF 전수 스캔 + Step 7 토큰화 시뮬레이션 결과 다음 2종 추가 reject 발견:

| 단어 | 원본 라인 | 누락 토큰 | 처리 |
|------|-----------|-----------|------|
| `each` | `each det./pron./adv. A1` | `det./pron./adv.` (슬래시 3중 조합) | ALLOWED_POS 추가 |
| `one` | `one number/det., pron. A1` | `number/det.` (number이 좌측) | ALLOWED_POS 추가 |

이 2종을 v4 ALLOWED_POS에 등재하지 않으면 `each`의 1행과 `one`의 1행(POS=`number/det.` 부분)이 reject 처리되어 손실.

**토큰화 로직** (오른쪽→왼쪽 CEFR 스캔):
1. 라인에서 CEFR 토큰 모두 찾기 (`A1|A2|B1|B2|C1`)
2. 각 CEFR 앞까지의 구간에서 POS 청크 추출
3. POS 청크 내 원자적 POS 우선 매칭 → 나머지를 쉼마로 분할
4. 각 POS × CEFR 조합을 별도 Row로 생성
5. 허용 목록 외 POS는 reject 로그

### Step 8. 특수 케이스 처리

#### `a, an indefinite article A1`
**v3 처리 위치 변경 유지**: Step 5.5의 `expand_multi_word_line()`이 라인을 `'a indefinite article A1'`, `'an indefinite article A1'` 두 라인으로 분리. 이후 Step 6 WORD_RE가 정상 처리. 결과: `a` + `indefinite article` + `A1`, `an` + `indefinite article` + `A1` 각각 별도 Row.

#### `have to modal v. A1`
`have to`를 공백 포함 단어로 그대로 `base_word`에 저장. WORD_RE가 `modal` 키워드 앞까지 단어로 인식하므로 자동 처리됨.

#### `each det./pron./adv. A1` (v4 명시)
슬래시 3중 POS는 분할하지 않고 그대로 `pos="det./pron./adv."`로 1행 생성. ALLOWED_POS에 등재되어 있으므로 reject 없음.

#### `one number/det., pron. A1` (v4 명시)
`number/det.`와 `pron.` 두 POS 토큰으로 분할 → 2행 생성 (`one number/det. A1`, `one pron. A1`).

#### 쌍 없는 homonym_num (후처리, Step 10에서)
Step 6에서는 일단 숫자 그대로 보존. 전체 파싱 완료 후 통합 데이터에서 재검증.

### Step 9. 5000 PDF 순차 스캔

```python
def parse_5000(pages: list) -> list[Row]:
    current_level = None
    rows = []
    for page_no, lines in pages:
        for lineno, line in enumerate(lines):
            stripped = line.strip()
            if not stripped or is_header(stripped):
                continue
            # 섹션 헤더
            if stripped in {"B2", "C1"}:
                current_level = stripped
                continue
            # artifact 제거
            stripped = strip_5000_artifact(stripped, current_level, ...)
            stripped = normalize(stripped)
            # 단어 추출 + POS 추출 (CEFR은 current_level로 강제)
            word_part, rest = extract_word_part(stripped)
            for pos_token in tokenize_pos(rest):
                rows.append(Row(..., cefr=current_level, source="Oxford 5000", ...))
    return rows
```

### Step 10. 전체 후처리 — 쌍 없는 homonym_num 정규화

⚠ **반드시 3000 + 5000 통합 후 수행**. 파일 경계를 넘는 쌍 존재:
- `minute1 n. A1` (Oxford 3000) + `minute2 adj. C1` (Oxford 5000) → 둘 다 유지
- `content1 n. B1` (Oxford 3000) + `content2 adj. C1` (Oxford 5000) → 둘 다 유지

```python
def postprocess_homonyms(rows: list[Row], log: list) -> list[Row]:
    # base_word별로 homonym_num 존재 셋 집계
    pairs = {}
    for r in rows:
        if r.homonym_num is not None:
            pairs.setdefault(r.base_word, set()).add(r.homonym_num)

    # 쌍이 없는 경우 (숫자가 1만 있거나 2만 있음) → word_raw는 유지, homonym_num만 NULL
    for r in rows:
        if r.homonym_num is not None and len(pairs.get(r.base_word, set())) < 2:
            log.append((r.base_word, r.homonym_num, "→ NULL"))
            r.homonym_num = None
    return rows
```

**쌍이 성립하는 단어 (homonym_num 유지) — v3·v4 실측 확정**:
3000 단독 쌍 (8개): `can`, `close`, `lie`, `live`, `ring`, `tear`, `used`, `wind`
파일 경계 쌍 (2개): `content` (3000+5000), `minute` (3000+5000)

**쌍 없는 단어 (NULL 전환 대상) — v3·v4 실측 확정**:
3000 only: `do`, `last`, `lead`, `long`, `plus`, `refuse`, `row`, `second`
5000 only: `bass`, `bow`, `pension`, `recount`

`last1`, `second1`처럼 2회 반복되지만 `*2`가 없는 경우 → 둘 다 NULL, disambiguation으로 UNIQUE 제약 유지.

### Step 11. SQLite + JSONL 기록

스키마는 §4 참조. **JSONL 저장 시 `ensure_ascii=False` 필수** (v4 명시):

```python
import json

with open("data/oxford_words.jsonl", "w", encoding="utf-8") as f:
    for row in rows:
        # ensure_ascii=False: 'résumé' 등 비-ASCII 단어를 \uXXXX 이스케이프 없이 그대로 저장
        # 가독성 + 로컬 LLM 컨텍스트 토큰 절감
        f.write(json.dumps(asdict(row), ensure_ascii=False) + "\n")
```

**v4 명시 근거**: 5000 PDF에 `résumé` (U+00E9 × 2) 1건 존재. 기본값 `ensure_ascii=True` 사용 시 `"r\u00e9sum\u00e9"`로 저장되어 (a) 사람 가독성 저하, (b) 로컬 LLM 토큰 사용량 증가, (c) JSON 파싱 후 후처리 코드에서 디코딩 누락 시 잠재 버그.

### Step 12. 검증 + 통계 출력

§6 체크리스트 전수 실행.

---

## 4. SQLite 스키마

```sql
CREATE TABLE words (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    word_raw       TEXT NOT NULL,     -- PDF 원본 표기 ("ring1", "light (not heavy)", "résumé")
    base_word      TEXT NOT NULL,     -- 숫자·괄호 제거 후 기준형
    disambiguation TEXT,              -- 괄호 내용, NULL 가능
    homonym_num    INTEGER,           -- 1, 2, NULL
    pos            TEXT NOT NULL,     -- "n.", "modal v.", "indefinite article", "det./pron./adv." 등
    cefr           TEXT NOT NULL,     -- "A1"~"C1"
    source         TEXT NOT NULL,     -- "Oxford 3000" | "Oxford 5000"
    source_page    INTEGER,           -- 원문 PDF 페이지 (1-based)
    source_order   INTEGER,           -- source 내 등장 순서
    raw_line_ref   TEXT               -- "p8:l120" 형태, 디버그용
);

CREATE UNIQUE INDEX idx_word_pos_cefr
    ON words(base_word, COALESCE(disambiguation,''), COALESCE(homonym_num,0), pos, cefr, source);

CREATE INDEX idx_base_word    ON words(base_word);
CREATE INDEX idx_cefr         ON words(cefr);
CREATE INDEX idx_pos          ON words(pos);
CREATE INDEX idx_source       ON words(source);
CREATE INDEX idx_source_order ON words(source, source_order);
```

**UNIQUE에 `source` 포함 이유**: `minute1 n. A1`(3000)과 `minute2 adj. C1`(5000)처럼 파일 경계 homonym 쌍을 둘 다 보존하기 위함. 그리고 B2에서 이론상 중복 가능성이 있을 경우 source로 구분.

### JSONL 포맷 (한 줄 = 한 행)

```jsonl
{"id":3,"word_raw":"ring1","base_word":"ring","disambiguation":null,"homonym_num":1,"pos":"n.","cefr":"A2","source":"Oxford 3000","source_page":8,"source_order":3001,"raw_line_ref":"p8:l120"}
{"id":5234,"word_raw":"résumé","base_word":"résumé","disambiguation":null,"homonym_num":null,"pos":"n.","cefr":"B2","source":"Oxford 5000","source_page":2,"source_order":337,"raw_line_ref":"p2:l563"}
```

---

## 5. 특수 케이스 통합 표 (P1~P19)

| # | 케이스 | 처리 단계 | 규칙 | 실측 건수 |
|---|--------|-----------|------|-----------|
| P1 | 멀티라인 타입 A (CEFR 없이 끊김) | Step 3 | trailing `,` or `/` + 다음 줄과 병합 | 3건 (3000) |
| P2 | 이전 분석 오류 — `like n. B1`은 유효 | Step 3 | 병합 후 정상 생성됨 | — |
| P3 | `noun.` 비표준 POS | Step 5 | `n.`으로 정규화 | 2건 (3000) |
| P4 | `a, an indefinite article A1` | Step 5.5 | `expand_multi_word_line()`로 라인 분리 | 1건 (3000) |
| P5 | `auxiliary v.` multi-word POS | Step 7 | ATOMIC_POS 우선 토큰화 | 3건 (3000: `be`, `do1`, `have`) |
| P6 | 슬래시 POS vs disambiguation 슬래시 | Step 6/7 | WORD_RE로 괄호 먼저 처리, `/` POS는 ALLOWED_POS에 등재 | 13종 40건 (v4) |
| P7 | `used1`/`used2` UNIQUE 제약 | §4 | `homonym_num`을 UNIQUE 컬럼에 포함 | 2건 (3000) |
| P8 | 쌍 없는 homonym_num | Step 10 | 통합 후 후처리, NULL 전환 + word_raw 유지 | 12건 (3000:8 + 5000:4) |
| P9 | 타입 C 멀티라인 (`double`) | Step 3 | **타입 A/B보다 먼저 적용** | 1건 (3000) |
| P10 | `last1`/`second1` 중복 | Step 10 | NULL 전환, disambiguation이 UNIQUE 방어 | 2그룹 (3000) |
| P11 | 과도한 공백 | Step 5 | `\s+` → ` ` | 2건 (`as`, `plus1`) |
| P12 | 파일 경계 homonym 쌍 | Step 10 | 통합 후 쌍 판정 | 2건 (`minute`, `content`) |
| P13 | 5000 섹션 헤더 오인 방지 | Step 3/9 | 멀티라인 병합은 3000 전용 | 헤더 5000:2건 / 3000 단독 CEFR:1건 |
| P14 | WORD_RE multi-word POS lookahead | Step 6 | `indefinite\|definite\|modal\|auxiliary\|number` 키워드 포함 | — |
| P15 | POS 마침표 누락 (`teen n., adj`) | Step 5 | `re.sub(r"\b(n\|v\|adj\|...)(?=\s*,\|\s*$)", r"\1.", line)` | 1건 (5000) |
| P16 | 스페셜 아포스트로피 (`o'clock`) | Step 5 | `line.replace("\u2019", "'")` | 1건 (3000) |
| **P17** | **슬래시 POS 주변 공백 (`det./ number`)** | **Step 5 (v4 신규)** | **`re.sub(r"\b([a-z]+)\.?\s*/\s*([a-z]+)\.?...", _normalize_slash_pos, line)`** | **1건 (3000, `second1`)** |
| **P18** | **슬래시 3중 POS (`det./pron./adv.`)** | **Step 7 (v4 신규)** | **ALLOWED_POS에 추가** | **1건 (3000, `each`)** |
| **P19** | **`number` 좌측 슬래시 POS (`number/det.`)** | **Step 7 (v4 신규)** | **ALLOWED_POS에 추가** | **1건 (3000, `one`)** |
| v2-1 | 5000 줄 끝 artifact (`castle n. B2`) | Step 4 | 현재 섹션과 동일한 trailing CEFR 제거 | 2건 (5000) |
| v2-2 | 헤더/푸터 | Step 2 | 정규식 목록으로 제거 | 3000:34건 / 5000:27건 |
| **v4-A** | **비-ASCII 단어 (`résumé`)** | **Step 11 (v4 신규)** | **JSONL 저장 시 `ensure_ascii=False`** | **1건 (5000)** |

---

## 6. 검증 체크리스트

스크립트 실행 후 `print_stats()`로 자동 출력:

**샘플 행 존재 검증** (모두 통과해야 함):
```
✅ march n. C1 (Oxford 5000)
✅ march v. C1 (Oxford 5000)
✅ ring homonym_num=1 → n. A2
✅ ring homonym_num=2 → v. A2, n. B1
✅ tear homonym_num=1 → v. A2 + n. B2 (2행)
✅ tear homonym_num=2 → n. B2
✅ address → n. A1 + v. B2 (2행)
✅ a → indefinite article A1
✅ an → indefinite article A1
✅ light (from the sun/a lamp) → n. A1, adj. A1, v. A2
✅ light (not heavy) → adj. A2
✅ double → adj./det./pron./v. A2 + adv. B1  [P9 검증]
✅ minute (base) → 1:n. A1(3000) + 2:adj. C1(5000), 둘 다 homonym 유지  [P12 검증]
✅ content (base) → 1:n. B1(3000) + 2:adj. C1(5000), 둘 다 homonym 유지  [P12 검증]
✅ last (base) → disambiguation으로 구분된 2그룹, homonym_num=NULL  [P10 검증]
✅ percent → n. A2, adj./adv. A2  [P3 검증]
✅ castle → n. B2 (B2 없는 일반 형태로 저장)  [v2-1 artifact 검증]
✅ teen → n. B2 + adj. B2 (2행)  [P15 검증]
✅ o'clock → adv. A1 (스페셜 아포스트로피 변환 후 정상 추출)  [P16 검증]
✅ part-time / full-time → adj./adv. B2  [슬래시 POS 보강 검증]
✅ second1 (next after the first) → det./number A1, adv. A2  [P17 v4 신규: 슬래시 공백 정규화 후 정상]
✅ each → det./pron./adv. A1 (1행, 슬래시 3중 그대로 보존)  [P18 v4 신규]
✅ one → number/det. A1 + pron. A1 (2행)  [P19 v4 신규]
✅ résumé → n. B2 (Oxford 5000, JSONL에 'résumé'로 그대로 저장)  [v4-A 신규]
✅ ALLOWED_POS 슬래시 POS 13종 모두 등장 + reject 0건  [v4 슬래시 보강 검증]
```

**집계 통계**:
```
- CEFR별 행 수 (A1/A2/B1/B2/C1)
- source별 행 수 (Oxford 3000 / Oxford 5000)
- source × CEFR 교차 분포
- POS 종류 목록 (ALLOWED_POS 중 실제 등장분)
- 멀티라인 병합 건수 (타입 A:3, B:2, C:1) 기대값
- 5000 artifact 제거 건수 (기대값: 2)
- 헤더/푸터 제거 건수 (기대값: 3000:34, 5000:27)
- homonym_num NULL 전환 건수 (기대값: 12건)
- 파일 경계 homonym 쌍 유지 건수 (기대값: 2 — minute, content)
- POS 마침표 복원 건수 (기대값: 1)
- 스페셜 아포스트로피 변환 건수 (기대값: 1)
- 슬래시 POS 공백 정규화 건수 [v4 신규] (기대값: 1, second1)
- 비-ASCII 단어 행 수 [v4 신규] (기대값: 1, résumé)
- a, an 다중 단어 라인 분리 건수 (기대값: 1)
- reject 로그 건수 (기대값: 0, 있으면 수동 점검)
```

**최종 품질 기준** (모두 만족 시 "성공"):
1. 위 샘플 행 검증 25건 전부 통과
2. reject 로그 0건
3. 멀티라인 병합 6건 정확히 실행 (타입 A:3, B:2, C:1)
4. 5000 artifact 제거 2건 정확히 실행
5. UNIQUE 제약 위반 0건
6. CEFR별 행 수가 A1+A2+B1+B2+C1 = 전체 행 수와 일치
7. 슬래시 POS 13종 모두 등장하고 ALLOWED_POS 미등재로 인한 reject 0건 (v4)
8. `teen` 단어가 `n.`과 `adj.` 두 행 모두 존재
9. `o'clock` 단어가 1행 정상 존재
10. `a`와 `an`이 각각 indefinite article로 별도 행 존재
11. **[v4] `second1` 단어가 `det./number A1` 행 정상 존재 (슬래시 공백 정규화 후)**
12. **[v4] `each` 단어가 `det./pron./adv. A1` 1행 존재**
13. **[v4] `one` 단어가 `number/det. A1` + `pron. A1` 2행 존재**
14. **[v4] `résumé` JSONL에 `"résumé"` 문자열 그대로 저장 (이스케이프 없음)**

---

## 7. 실행 명령

```bash
cd vocabulary
python scripts/build_db.py
# 기대 출력: 모든 ✅ 체크, 집계 통계, "Build successful"
```

DB 조회 검증:
```bash
sqlite3 data/oxford_words.db "SELECT * FROM words WHERE base_word='ring' ORDER BY homonym_num, cefr;"
sqlite3 data/oxford_words.db "SELECT cefr, source, COUNT(*) FROM words GROUP BY cefr, source;"
sqlite3 data/oxford_words.db "SELECT * FROM words WHERE pos LIKE '%/%' ORDER BY pos;"  -- v4 슬래시 POS 점검
sqlite3 data/oxford_words.db "SELECT * FROM words WHERE base_word='résumé';"  -- v4 비-ASCII 점검
```

---

## 8. 구현 시 주의사항

1. **실행 순서 불변**: Step 2 → Step 3 (3000만) → Step 4 (5000만) → Step 5 → Step 5.5 → Step 6 → Step 7 → Step 8 → Step 10. 순서 바꾸면 파싱 실패.
2. **빈 줄 보존**: Step 2에서 빈 줄 제거 금지 (Step 3 멀티라인 탐지가 "다음 비어있지 않은 줄"을 찾기 때문).
3. **정규화는 병합 후**: Step 5는 Step 3 이후에 적용. 병합 전에 trailing whitespace를 제거하면 타입 C 탐지가 영향을 받을 수 있음.
4. **Step 5 내부 순서 (v4)**: 스페셜 아포스트로피 변환 → adj.B1 공백 → noun.→n. → POS 마침표 복원 → **슬래시 POS 공백 정규화 (v4 신규)** → 공백 정규화. 슬래시 POS 정규화는 POS 마침표 복원 이후에 와야 좌·우 양쪽 토큰이 마침표 없이도 매칭 가능 (정규식 `\.?` 포함).
5. **Step 5.5는 Step 6 직전**: `expand_multi_word_line()`이 라인을 복수로 늘릴 수 있으므로, 이후 단계는 늘어난 각 라인을 개별 처리.
6. **Step 11 인코딩 (v4)**: JSONL은 `open(..., encoding="utf-8")` + `json.dumps(..., ensure_ascii=False)`. SQLite는 기본적으로 UTF-8이므로 별도 설정 불필요.
7. **debug 로그 필수**: 실패 시 재현·디버깅 근거. log 없이 DB만 있으면 오류 추적 불가.
8. **DB 재생성은 항상 전체**: partial update 금지. idempotent하게 매번 `DROP TABLE` → `CREATE` → 재삽입.

---

## 9. 선행 문서 대비 변경 요약

| 항목 | v1 (기존) | v2 (보강) | v3 (이전) | **v4 (현재)** |
|------|-----------|-----------|-----------|--------------|
| 멀티라인 패턴 수 | A/B (5건) | A/B (5건) | A/B/C (6건) | A/B/C (6건) |
| 5000 artifact | 미언급 | 제거 규칙 정의 | 정확히 2건 실측 | 2건 + B2/C1 외 없음 검증 |
| 공백 정규화 | 부분 | 부분 | 명시적 `\s+` → ` ` | + 슬래시 POS 주변 공백 |
| 후처리 파일 경계 | 파일별 | 통합 후 판단 | minute + content 2건 | 2건 (변동 없음) |
| UNIQUE에 source | 없음 | 있음 | 있음 | 있음 |
| source_page/order 추적 | 없음 | 있음 | 있음 | 있음 |
| reject 로그 | 없음 | 있음 | 있음 | 있음 |
| 실행 순서 | 느슨 | 느슨 | Step 0~12 순번 명시 | 순번 명시 + Step 5 내부 순서 명시 |
| 샘플 검증 케이스 | 12건 | 17건 | 21건 | **25건** |
| ALLOWED_POS 슬래시 | 없음 | 4종 | 11종 | **13종 (v4에서 2종 추가)** |
| `a, an` 처리 위치 | Step 8 | Step 8 | Step 5.5 | Step 5.5 |
| POS 마침표 복원 | 없음 | 없음 | 있음 (P15) | 있음 |
| 스페셜 아포스트로피 처리 | 없음 | 없음 | 있음 (P16) | 있음 |
| **슬래시 POS 공백 정규화** | 없음 | 없음 | 없음 | **있음 (P17)** |
| **슬래시 3중 POS 등재** | 없음 | 없음 | 없음 | **있음 (P18, each)** |
| **`number` 좌측 슬래시 POS** | 없음 | 없음 | 없음 | **있음 (P19, one)** |
| **JSONL ensure_ascii=False** | 미명시 | 미명시 | 미명시 | **명시 (v4-A, résumé)** |
| **v2 §10 부록 복원** | (v2 §10 존재) | §10 280줄 분석 | **통째로 제거** | **§12 부록으로 복원** |

---

## 10. v3 → v4 변경 요약 (실측 결함 3건 + 1건 명시)

v3 출시 후 PDF 원본 + Step 7 토큰화 시뮬레이션 전수 재실측에서 발견된 결함 3건 + 1건 명시 사항을 반영.

### v3에서 누락된 처리 (신규)
1. **P17 — `second1` 멀티라인 병합 결과 `det./ number` 슬래시 공백 (3000 PDF)**: Step 5에 슬래시 POS 주변 공백 정규화 정규식 추가.
   미반영 시 `det./ number`가 ALLOWED_POS 미등재로 reject 처리되어 `second1 (next after the first)` 단어의 `det./number A1` 행 1건 손실.

2. **P18 — `each det./pron./adv. A1` 슬래시 3중 조합 (3000 PDF)**: Step 7 ALLOWED_POS에 `det./pron./adv.` 추가.
   미반영 시 `each` 단어 행 1건이 reject되어 손실.

3. **P19 — `one number/det., pron. A1`에서 `number` 좌측 슬래시 POS (3000 PDF)**: Step 7 ALLOWED_POS에 `number/det.` 추가.
   미반영 시 `one` 단어의 `number/det. A1` 행 1건이 reject되어 손실 (`pron. A1` 행은 정상 처리됨).

### v3 미명시 사항 (v4에서 명시)
4. **v4-A — `résumé n.` (5000 PDF) JSONL 저장 시 `ensure_ascii=False`**: Step 11에 명시.
   미명시 시 JSONL에 `"r\u00e9sum\u00e9"`로 이스케이프되어 저장 → (a) 가독성 저하, (b) 로컬 LLM 토큰 사용량 증가, (c) 후처리 코드의 디코딩 누락 잠재 버그.
   **WORD_RE 자체는 정상 매칭되어 SQLite에는 정상 저장됨** (Python `\w`가 유니코드 매칭). 단지 JSONL 저장 옵션이 명시되지 않은 것.

### v3가 잘라낸 v2 §10 정책 — v4에서 부록으로 복원 (§12)
5. **v2 §10 (280줄)이 v3에서 통째로 제거됨**: 외부 비판 5건 분석·정책 결정·방어적 fallback·회귀 테스트·OCR 범위 밖 정책이 모두 누락.
   v3는 그 중 P15(POS 마침표 lookahead) · P16(스페셜 아포스트로피)만 부분 흡수.
   **v4 §12 부록**으로 v2의 누락된 5개 정책을 체계적으로 복원:
   - §12.2-(a) 쉼표 오타 fallback (`adj,` → `adj.`)
   - §12.2-(b) CEFR-직전 무마침표 fallback (`v A2` → `v. A2`)
   - §12.2-(c) 공백 POS 연쇄 opt-in fallback (`v n.` → `v., n.`)
   - §12.3 회귀 테스트 8건 (v2 §10.6 4건을 8건으로 확장)
   - §12.4 OCR/인코딩 복구 범위 밖 정책 명시

### 호환성
- 스키마, 산출물 경로, SQLite UNIQUE 인덱스 정의는 v3와 100% 호환.
- v4 신규 정규화/등재 단계는 모두 **추가**일 뿐이며, v3에서 정상 처리되던 라인을 깨뜨리지 않음.
- `build_db.py`를 v3 → v4로 업그레이드 시 **DB 전체 재생성** 권장 (Step 5 정규화 변경 + Step 7 ALLOWED_POS 변경으로 일부 행이 새로 생성될 수 있음).

### v4 실행 시 새로 통과해야 하는 검증
- `second1 (next after the first)` 행에 `pos="det./number"` + `cefr="A1"` 존재
- `each` 행에 `pos="det./pron./adv."` + `cefr="A1"` 1건 존재
- `one` 행에 `pos="number/det."` + `cefr="A1"` 1건 존재 (+ `pron. A1` 1건)
- `résumé` JSONL 라인에 `"résumé"` 문자열 그대로 저장 (`\u00e9` 이스케이프 없음)
- ALLOWED_POS 슬래시 13종 모두 등장하고 미등재로 인한 reject 0건

---

## 11. 외부 LLM 분석 4건 — 모두 거짓 검증 (v4 명시)

v3 검증 과정에서 외부 LLM으로부터 다음 4건의 결함 주장을 받았으나, **PDF 원본을 pdfminer로 직접 추출하여 실측한 결과 4건 모두 거짓**으로 확인됨. 향후 동일한 분석이 반복될 경우를 대비해 검증 결과를 명시함.

| # | 외부 LLM 주장 | 실측 결과 (pdfminer.six 추출) | 진위 |
|---|---------------|------------------------------|------|
| 1 | 5000 PDF에 `absorb v`, `alien n`, `arm v` 등 POS 마침표 누락 | `'absorb v.'`, `'alien n.'`, `'alien adj.'`, `'arm v.'` (모두 마침표 정상) | **거짓** |
| 2 | 5000 PDF에 `crack v n.`, `screw v  n.` 등 다중 POS 사이 쉼표 누락 | `'crack v., n.'`, `'screw v., n.'` (쉼표·마침표 모두 정상) | **거짓** |
| 3 | 동음이의어가 위첨자(`content²`, `minute²`) 또는 아포스트로피(`bow'`, `recount'`) 표기 | `'content2 adj.'`, `'minute2 adj.'`, `'bow1 v., n.'`, `'recount1 v.'` (모두 일반 ASCII 숫자) | **거짓** |
| 4 | 3000 PDF의 `light` 항목이 `adj,` (쉼표) 표기로 마침표 오타 | `'light (from the sun/a lamp) n.,'` + 다음 줄 `'adj. A1, v. A2'` (마침표 정상, 단순히 멀티라인 패턴 A) | **거짓** |

### 원인 분석
외부 LLM은 PDF의 **시각적 표시(rendering)** 텍스트와 **pdfminer가 실제 추출하는 텍스트** 사이의 차이를 혼동한 것으로 보임:
- PDF 화면에서 위첨자처럼 보이는 숫자도 pdfminer는 일반 ASCII 숫자로 추출 (Oxford 폰트의 작은 숫자 표기 컨벤션).
- PDF 화면에서 마침표가 안 보이거나 작게 표시되는 경우도 pdfminer 추출 결과에는 정확히 포함됨.
- PDF 화면에서 쉼표·마침표 구분이 어려운 경우도 실제 텍스트 데이터에는 정확히 구분됨.

### v4 검증 원칙
**모든 가이드 변경은 반드시 `pdfminer.six`의 `extract_text()` 결과를 기준으로 한다.** PDF 뷰어 화면 표시, 다른 PDF 파서 결과, LLM의 PDF 시각 분석은 보조 참고용일 뿐 단일 진실 원본(SoT)이 될 수 없음. v3·v4의 모든 결함(P15, P16, P17, P18, P19, v4-A)은 이 원칙으로 발견되고 검증됨.

> **참고**: 위 4건은 v2 §10에서 이미 한 차례 동일한 비판 5건으로 검토된 적이 있다. v3 작성자가 v2 §10을 통째로 잘라내면서 그 정책 결정 이력이 사라졌으므로, v4에서는 §12 부록으로 v2 §10의 누락 정책(방어적 fallback, 회귀 테스트, OCR 범위 밖 정책)을 복원한다.

---

## 12. 부록 — v2 §10 누락 정책 복원 (v4 신규)

### 12.1 배경

v2 가이드에는 §10 (`추가 분석 기록 — 비판 5개 항목 재평가`)에 외부 비판 5건에 대한 분석과 정책 결정이 약 280줄로 포함되어 있었다. v3 작성자가 이 섹션을 **통째로 제거**하면서 일부만 P15(POS 마침표 lookahead) · P16(스페셜 아포스트로피)으로 흡수했고, 나머지는 누락되었다.

| v2 §10 항목 | v2 정책 결정 | v3 반영 | v4 (본 부록) 처리 |
|------------|-------------|---------|------------------|
| ① 무마침표 POS (`absorb v`) | "현재 PDF엔 없으나 fallback 추가 권장" | 부분 (P15 lookahead, 1건만 잡음) | §12.2-(b) CEFR-직전 fallback 보강 |
| ② 공백 POS 연쇄 (`crack v n.`) | "현재 PDF엔 없으나 opt-in fallback" | 미반영 | §12.2-(c) opt-in fallback 명시 |
| ③ OCR 인코딩 (`сор п.`) | "**범위 밖**으로 정책 분리" | 미명시 | §12.4 범위 밖 정책 복원 |
| ④ 쉼표 오타 (`adj,`) | "**강하게 타당**, fallback 필수" | 미반영 | §12.2-(a) 쉼표 오타 fallback 추가 |
| ⑤ 무마침표 POS 일반 | "Step 5.5 보강 필요" | 부분 (P15) | §12.2 통합 |

**v3가 잘라낸 이유 추정**: v2 §10.5의 코드는 단어 본체 `false positive` 위험이 있어 (예: `\b{pos},` 전역 치환은 단어 안의 `n,` 같은 부분도 변환할 수 있음) v3가 안전한 lookahead 방식만 선별한 것으로 보인다. 이는 합리적 결정이지만 **정책 이력 자체가 문서에서 사라진 것**이 문제. v4는 정책 이력을 보존하면서 안전한 형태로 fallback을 명시한다.

### 12.2 방어적 fallback — Step 5의 보강 sub-step

v3 P15는 `\b(n|v|adj|...)` + lookahead `(?=\s*,|\s*$)` 로 **쉼표/줄끝 직전**만 정확히 잡는다. 이는 `teen n., adj` 같은 케이스는 잡지만, 아래 케이스는 못 잡는다.

| 패턴 | v3 P15 매칭 | v4 §12.2 보강 |
|------|------------|---------------|
| `teen n., adj` (쉼표/줄끝 직전) | ✅ | ✅ (변동 없음) |
| `dare v A2` (CEFR 직전 무마침표) | ❌ | ✅ — (b) |
| `light adj, A2` (POS 직후 쉼표 오타) | ❌ | ✅ — (a) |
| `chat v n. A2` (공백 POS 연쇄) | ❌ | ⚠ opt-in — (c) |

**현재 PDF 기준**: 위 (a)/(b)/(c) 모두 0건. v3가 이 보강을 누락해도 현재 PDF 처리에는 영향 없음. 그러나 **PDF 갱신·추출기 변경·후속 텍스트 정제 과정에서 마침표·쉼표 손상이 생길 경우** v3는 무방비.

```python
BASIC_POS = ["n", "v", "adj", "adv", "prep", "conj", "pron", "det", "exclam"]

def repair_pos_punctuation(line: str, *, enable_aggressive_repair: bool = False) -> str:
    """v2 §10.5 정책 흡수 — Step 5의 P15 이후, 슬래시 정규화 직전에 적용.
    
    enable_aggressive_repair=False가 기본값.
    True로 설정 시 (c) 공백 POS 연쇄 fallback도 적용 (false positive 위험).
    """
    # (a) 쉼표 오타 복구: 'adj,' → 'adj.'
    # POS 약어 직후의 쉼표 + 공백/줄끝만 변환. 단어 본체 'and,' 등은 영향 없음.
    for pos in BASIC_POS:
        line = re.sub(rf"\b{pos},(?=\s|$)", f"{pos}.", line)
    
    # (b) CEFR 직전 무마침표 POS 복구: 'dare v A2' → 'dare v. A2'
    # v3 P15와 보완적 — v3는 줄끝/쉼표 직전, v4 (b)는 CEFR 직전.
    for pos in BASIC_POS:
        line = re.sub(
            rf"\b{pos}(?=\s+(A1|A2|B1|B2|C1)\b)", f"{pos}.", line
        )
    
    # (c) 공격적 fallback (opt-in): 공백만으로 이어진 POS 연쇄
    # 'chat v n. A2' → 'chat v., n. A2'
    # WARNING: 단어 본체에 'v n' 같은 우연한 시퀀스가 있으면 false positive.
    # 따라서 환경변수 또는 명시적 플래그로만 활성화 권장.
    if enable_aggressive_repair:
        line = re.sub(r"\bv\s+n\.", "v., n.", line)
        line = re.sub(r"\bn\s+v\.", "n., v.", line)
        line = re.sub(r"\badj\s+adv\.", "adj., adv.", line)
        line = re.sub(r"\badv\s+adj\.", "adv., adj.", line)
    
    return line
```

**Step 5 내부 순서 업데이트 (v4 부록 반영)**:
1. 스페셜 아포스트로피 변환 (P16)
2. `adj.B1` 공백 (`(\.)([A-Z][0-9])`)
3. `noun.` → `n.` (P3)
4. POS 마침표 복원 lookahead (P15 — v3 도입)
5. **`repair_pos_punctuation()` 적용 (§12.2 — v4 부록)** ← 신규 위치
6. 슬래시 POS 공백 정규화 (P17 — v4 본문)
7. `\s+` → ` ` 공백 정규화

### 12.3 회귀 테스트 (v2 §10.6 흡수)

v2 §10.6에서 정의한 4개 회귀 테스트를 v4에서 명시적으로 보존. **인위적 손상 입력**으로 정규화·매칭 강건성 검증 (실제 PDF 케이스가 아님).

```python
def run_regression_tests():
    """v2 §10.6 흡수 — 파서 견고성 회귀 테스트.
    실제 PDF에 없는 인위적 입력으로 §12.2 fallback 동작 확인."""
    
    cases = [
        # 1. 기존 정상 케이스 변동 없음
        ("part-time adj./adv. B2", True, False),
        ("full-time adj./adv. B2", True, False),
        ("résumé n.", True, False),
        
        # 2. 무마침표 POS (인위적 손상) → §12.2-(b) 정규화 후 통과
        ("absorb v B2", False, True),   # before: ❌, after normalize: ✅
        ("alien n B2", False, True),
        ("dare v B2", False, True),
        
        # 3. 쉼표 오타 (인위적 손상) → §12.2-(a) 정규화 후 통과
        ("light adj, A2", False, True),
        
        # 4. 공백 POS 연쇄 (aggressive opt-in) → §12.2-(c) 정규화 후 통과
        ("chat v n. A2", False, True),  # enable_aggressive_repair=True 필요
    ]
    
    for line, expect_before, expect_after in cases:
        before_ok = WORD_RE.match(line) is not None
        normalized = repair_pos_punctuation(
            normalize(line),
            enable_aggressive_repair=("v n." in line or "n v." in line),
        )
        after_ok = WORD_RE.match(normalized) is not None
        
        assert before_ok == expect_before, f"BEFORE 기대 불일치: {line}"
        assert after_ok == expect_after, f"AFTER 기대 불일치: {line} → {normalized}"
    
    print("✅ v2 §10.6 회귀 테스트 8건 모두 통과")
```

### 12.4 OCR/인코딩 복구 — 범위 밖 정책 (v2 §10.2 ③ 복원)

v2가 명시적으로 결정한 정책을 v4에서도 보존:

> **본 가이드는 OCR/인코딩 복구를 요구사항으로 포함하지 않는다.**
> - 본 가이드는 `pdfminer.six`로 안정적으로 추출되는 텍스트를 전제로 한다.
> - 키릴 문자 OCR 깨짐 (예: `cop n.` → `сор п.`) 같은 인코딩 손상은 본 가이드 범위 밖이다.
> - 향후 스캔 PDF 또는 OCR 입력을 지원해야 한다면, **별도의 pre-OCR normalization 파이프라인 문서**를 분리하여 관리한다.

**근거**: 현재 업로드된 두 PDF는 pdfminer로 정상 추출되며, 비-ASCII 문자도 단 1종(`é` × 2, `'` × 1)에 불과 (§11 검증 결과). OCR 복구 로직을 본 가이드에 포함하면 현재 입력에 대한 false positive 위험만 늘어나고 실익은 없다.

### 12.5 §10·§12 변경 관계 정리

| 정책 항목 | v2 위치 | v3 처리 | v4 위치 |
|----------|--------|---------|--------|
| POS 마침표 lookahead | 미존재 | Step 5 P15 신규 | Step 5 P15 (유지) |
| 스페셜 아포스트로피 | 미존재 | Step 5 P16 신규 | Step 5 P16 (유지) |
| 슬래시 POS 11종 보강 | 4종만 | 11종 등재 | 13종 등재 (P18·P19 추가) |
| **POS 쉼표 오타 fallback** | §10.5 (a) | **누락** | **§12.2-(a) 복원** |
| **CEFR-직전 무마침표 fallback** | §10.5 (b) | **누락** | **§12.2-(b) 복원** |
| **공백 POS 연쇄 fallback** | §10.5 (c) | **누락** | **§12.2-(c) opt-in 복원** |
| **회귀 테스트 4건** | §10.6 | **누락** | **§12.3 8건으로 확장 복원** |
| **OCR 범위 밖 정책** | §10.2 ③ | **미명시** | **§12.4 명시 복원** |

---

**결론**: v4 가이드대로 구현하면 PDF 두 개 → 통합 DB 생성 시 알려진 결함 0건. v3까지 누적된 모든 실측 검증 + v4 본문 추가 결함 3건(P17·P18·P19) + 명시 사항 1건(v4-A) + 외부 LLM 분석 검증 4건(§11) + v2 §10 누락 정책 복원 5건(§12)을 모두 반영. 

**v4의 두 가지 안전망**:
1. **본문 (§3~§10)**: 현재 PDF 기준 reject 0건 보장 (P17·P18·P19로 v3 결함 봉인).
2. **부록 (§12)**: PDF 갱신·추출기 변경·텍스트 손상 시 자동 흡수 (v2 §10 정책 보존).
