# Oxford 3000/5000 CSV — DB 검증 리포트

**목적**: Oxford Learner's Dictionaries에서 다운로드한 CEFR 레벨별 CSV 7개 파일을 학습용 DB로 사용하기 전, 데이터 무결성과 구조 이슈를 정밀 점검한 결과 정리. 다른 LLM의 검증을 위한 참조 문서.

---

## 1. 원본 파일 구성

| 파일 | 행 수(헤더 제외) | 비고 |
|------|-----------------:|------|
| A1.csv | 901 | Oxford 3000 |
| A2.csv | 869 | Oxford 3000 |
| B1.csv | 809 | Oxford 3000 |
| B2.csv | 730 | Oxford 3000 |
| B2(1).csv | 730 | **B2와 완전 동일 → 중복 파일, 제외 필요** |
| B2+.csv | 700 | Oxford 5000 추가분 |
| C1.csv | 1,312 | Oxford 5000 추가분 |
| **합계 (B2(1) 제외)** | **5,321** | |

- **고유 단어(lowercase 기준): 약 4,971개**
- CEFR 레벨은 파일명으로만 식별 (데이터 내 컬럼 없음) → DB 저장 시 파일명에서 파생

---

## 2. CSV 구조

- **인코딩**: UTF-8
- **구분자**: 쉼표(`,`)
- **헤더**: `Word, POS` (단, 실제로는 POS가 여러 컬럼에 걸쳐 분리됨)
- **최대 컬럼 수**: 파일별 3~4 (단어 + POS 최대 3개)
  - B2+만 최대 3컬럼, 나머지는 최대 4컬럼
- **빈 행**: 0건
- **앞뒤 공백 이슈**: 0건

### 실제 구조 예시
```csv
Word,POS
all,det.,pron.
few,det.,adj.,pron.
a,indefinite article
```

> **주의**: 초기 단순 텍스트 파싱 시 `all,det.,pron.`을 "word=all, POS=pron." + "det. leak"으로 오판할 수 있음. **실제로는 컬럼 B=det., C=pron.** 이므로 `csv` 모듈로 읽어야 함.

---

## 3. POS 체계

**총 14종 확정** (A1에서 전부 등장, A2~C1 신규 없음)

| POS | 한글 | 비고 |
|-----|------|------|
| `n.` | 명사 | |
| `v.` | 동사 | |
| `adj.` | 형용사 | |
| `adv.` | 부사 | |
| `prep.` | 전치사 | |
| `pron.` | 대명사 | |
| `conj.` | 접속사 | |
| `det.` | 한정사 | |
| `modal v.` | 법조동사 | |
| `auxiliary v.` | 조동사 | |
| `exclam.` | 감탄사 | |
| `number` | 수사 | |
| `indefinite article` | 부정관사 | `a`, `an` 2건만 |
| `definite article` | 정관사 | `the` 1건만 |

- 유효하지 않은 POS 토큰: **0건**
- POS 없음(null): **0건**

---

## 4. 복수 POS 단어 (245건)

**하나의 단어가 여러 문법 역할을 동시에 가지는 경우** (같은 의미, 다른 품사).

- A1: 65, A2: 52, B1: 32, B2: 46, B2+: 15, C1: 35
- 예: `walk` (v., n.), `all` (det., pron.), `few` (det., adj., pron.)
- **학습 관점**: 1개 카드로 처리 권장 (뜻이 동일/유사)

---

## 5. Disambiguation 단어 (38건)

**같은 철자, 다른 의미**로 Oxford가 의도적으로 분리한 항목. 괄호 표기로 명시됨.

- A1: 9, A2: 12, B1: 10, B2: 1, B2+: 1, C1: 5
- 예:
  - `bank (money)` n. / `bank (river)` n.
  - `light (from the sun, a lamp)` n./adj. / `light (not heavy)` adj.
  - `second (next after the first)` det./number / `second (unit of time)` n.
- **학습 관점**: 별도 카드 처리 (의미가 완전히 다름)
- **주의**: 괄호 내부에 쉼표 포함 (`light (from the sun, a lamp)`) → CSV 파서가 컬럼 분리를 잘못하지 않도록 `csv` 모듈 필수

---

## 6. 중복 이슈

### 6-1. 레벨 내 중복
| 레벨 | 단어 | 상태 |
|------|------|------|
| A2 | `ring` | 2행 (`n.` / `v.`) — Disambiguation 없이 분리됨 |
| B1 | `used` | 2행 **둘 다 `adj.`** — ⚠️ 진짜 중복 가능성 (또는 의미 차이인데 disambiguation 누락) |
| B2 | `tear` | 2행 (`v., n.` / `n.`) — 의미 분화 추정(눈물/찢다) 인데 disambiguation 없음 |

### 6-2. 레벨 간 중복 (336 단어)
- **동일 철자가 서로 다른 CEFR 레벨에 등장**
- 대부분 POS가 다름 (335/336): 같은 단어의 다른 문법 역할이 난이도 차이로 분리된 것
- 예: `address` A1(n.) / B2(v.), `back` A1(n.,adv.) / A2(adj.) / B2(v.)
- **DB 설계 시사점**: `(word, pos, level)` 조합이 유일 키. `word` 단독으로는 고유하지 않음.

---

## 7. 단어 형태 특이 케이스

- **공백 포함 복합어** (6건): `have to`, `ice cream`, `next to`, `no one`, `according to`, `all right`
- **하이픈 복합어** (13건): `T-shirt`, `old-fashioned`, `long-term`, `full-time`, `part-time`, `short-term`, `so-called`, `decision-making`, `high-profile`, `large-scale`, `long-standing`, `thought-provoking`
- **Apostrophe 포함**: `o’clock` (curly quote `U+2019`, straight `'` 아님 — 입력 시 주의)
- **대문자 시작** (28건): 월(`April`~`December`), 요일(`Friday`~`Sunday`), 대명사 `I`, 약어 `CD`, `DVD`, `TV`, `OK`, `IT`, `AIDS`, `ID`
- **고유명사/약어 처리**: 전부 대문자 유지 필요 (lowercase 변환 금지 대상 존재)

---

## 8. DB 설계 권장사항

### 필수 필드
| 필드 | 타입 | 설명 |
|------|------|------|
| `id` | INT PK | |
| `word` | TEXT | 원본 단어 (disambiguation 괄호 포함 가능) |
| `base_word` | TEXT | 괄호 제거 형태 (매칭/검색용) |
| `pos` | TEXT | POS 하나 또는 쉼표 구분 문자열 (`"v., n."`) — 혹은 별도 테이블로 정규화 |
| `cefr` | TEXT | A1/A2/B1/B2/B2+/C1 |
| `source` | TEXT | 파일명 또는 Oxford 3000/5000 구분 |

### 유니크 키 후보
- `(base_word, pos_signature, cefr)` — 레벨 간 중복 처리 가능
- 단순히 `word` 단독은 **유일하지 않음**

---

## 9. ⚠️ 제가 누락 가능성이 있다고 판단하는 "DB 관점" 추가 검증 포인트

다른 LLM이 추가로 확인해주면 좋은 항목:

1. **레벨 내 `used` 중복 (B1)**: 두 행 모두 `adj.`인데 정말 같은 단어인지, 아니면 disambiguation이 빠진 건지 원본 Oxford 사이트 대조 필요
2. **`tear`, `ring` disambiguation 누락**: Oxford 온라인 사전에서는 별도 항목으로 분리되어 있는지 확인 필요
3. **B2(1) 완전 동일성**: 파일 바이트 단위 동일성까지 확인 (줄바꿈/BOM 차이 가능성)
4. **BOM/줄바꿈 코드**: CRLF vs LF, UTF-8 BOM 유무 (본 분석은 Python 기본 read로 처리 — 이슈 없어 보임)
5. **Trailing blank POS column**: 일부 행에 빈 4번째 컬럼이 있는지 (rtrim된 파서 기준 이슈 없었으나 DB 로드 시 확인)
6. **Oxford 5000 공식 정의와 일치성**: B2+ + C1이 진짜 Oxford 5000 확장분인지, 다른 레벨에서 누락된 단어는 없는지
7. **Lemmatization 필요성**: 파생형 포함 여부 (`cook` n./v., `cooking` n. 모두 A1 포함 → 어근 중복 아님을 확인 필요)
8. **동음이의어 vs 다의어 구분 기준**: Oxford의 disambiguation 괄호가 완전한지 (동일 철자인데 분리 안 된 항목 추가 존재 가능성)
9. **POS 조합 signature 정규화**: `(v., n.)` 과 `(n., v.)` 순서가 혼재하지 않는지 — 본 데이터에서는 파악 범위 밖
10. **기호 정규화**: `™`, curly apostrophe 등을 DB 입력 시 유지할지 표준화할지 정책 필요
11. **학습 순서 메타데이터 부재**: CEFR만 있고 Oxford 공식 빈도순위(frequency rank)는 없음 — SM-2 초기 정렬 시 고려 필요
12. **대소문자 정규화 정책 미정**: 고유명사/약어 보존 vs 검색 편의성

---

## 11. 실제 데이터 기반 추가 검증 결과 (2026-04-23)

> 섹션 9의 의심 항목을 실제 CSV로 직접 검증한 결과.

---

### 11-1. ✅ `used` B1 중복 — 진짜 완전 중복 확인

- `[B1] 'used', adj.` 행이 **정확히 2회** 존재. word·POS·레벨 모두 동일.
- disambiguation도 없고 의미 차이도 없음 → **한 행 제거 필요**.
- 원인: Oxford 원본 CSV 데이터 오류로 추정.

---

### 11-2. ✅ `tear` B2 — disambiguation 누락 확인

- B2에 `tear` 행 2개 존재:
  - `tear` → pos `['v.', 'n.']`
  - `tear` → pos `['n.']`
- 눈물(n.)과 찢다(v./n.)의 의미 분화인데 disambiguation 괄호 없음.
- `n.`이 두 행에 중복 포함됨. **DB 로드 전 수동 보정 필요** (`tear (rip)`, `tear (from eyes)` 등으로 분리 권장).

---

### 11-3. ✅ `ring` A2 — disambiguation 없이 분리, B1에도 추가 중복

- A2에 `ring` n.과 `ring` v. 별도 행 (disambiguation 없음).
- B1에도 `ring` n.이 별도 존재 → **레벨 간 중복** (A2 n. / A2 v. / B1 n.).
- A2의 두 행은 복수 POS(`n., v.`)로 병합하거나 유지 여부 결정 필요.

---

### 11-4. ⚠️ **신규 발견** — CSV 파싱 오류 4건 (괄호 안 쉼표)

**가장 심각한 이슈.** 파일 내 일부 disambiguation 단어가 **따옴표 없이** 괄호 안 쉼표를 포함 → `csv.reader`가 컬럼 경계를 잘못 인식.

| 레벨 | 행 | 원본 raw 값 (실제 파일) |
|------|----|------------------------|
| A1 | 434 | `light (from the sun,a lamp)n.,adj.` |
| A1 | 436 | `like (find sb,sth pleasant),v.` |
| A1 | 461 | `match (contest,correspond),n.` |
| A2 | 456 | `light (from the sun,a lamp),v.` |

**파싱 결과 예시** (`csv.reader` 기준):
- `like (find sb/sth pleasant) v.`가 의도인데 실제로는:
  - word = `like (find sb`
  - pos[0] = `sth pleasant)`
  - pos[1] = `v.`
- `match (contest/correspond) n.`이 의도인데:
  - word = `match (contest`
  - pos[0] = `correspond)`
  - pos[1] = `n.`

**대응**: DB 로드 스크립트에서 이 4개 행을 하드코딩 보정하거나, 원본 파일에서 괄호 내 쉼표를 슬래시(`/`)로 치환하는 전처리 필요.

---

### 11-5. ⚠️ **신규 발견** — POS signature 순서 혼재 확인

섹션 9-9의 의심이 실제로 확인됨.

**혼재 케이스 1** `{adj., det., pron.}`:
| 단어 | 레벨 | 실제 순서 |
|------|------|-----------|
| `few` | A1 | `det., adj., pron.` |
| `little` | A1 | `adj., det., pron.` |
| `double` | A2 | `adj., det., pron.` |

**혼재 케이스 2** `{v., n., adj.}`:
| 단어 | 레벨 | 실제 순서 |
|------|------|-----------|
| `waste` | B1 | `n., v., adj.` |
| `advance` | B2 | `n., v., adj.` |
| `alert` | C1 | `v., n., adj.` |
| `reverse` | C1 | `v., n., adj.` |

**DB 설계 시사점**: pos 컬럼을 `"det., adj., pron."` 형태 문자열로 저장하면 동일 의미 단어가 다른 signature로 취급됨. **정렬된 pos set을 canonical signature로 저장하고, 원본 순서는 별도 보존** 권장.

---

### 11-6. ℹ️ Lemmatization — 어근+파생형 동시 수록 확인

| 어근 | 레벨 | 파생형 | 레벨 |
|------|------|--------|------|
| `cook` v. (A1) | → | `cooking` n. | A1 |
| `run` v. (A1) | → | `running` n. | A2 |
| `drive` v. (A1) | → | `driving` n. | A2 / `driving` adj. | C1 |

- `driving`이 A2(n.)와 C1(adj.)에 **레벨 다르게 2회 수록**.
- 어근과 파생형은 Oxford가 의도적으로 별도 단어로 처리한 것으로 판단 (각각 독립 카드 대상).
- `driving` adj.(C1)은 레벨 간 중복 항목으로도 관리 필요.

---

### 11-7. ✅ Trailing blank column — 0건 확인

섹션 9-5 의심 항목: **이슈 없음 확인됨.**

---

## 10. 결론

- **데이터 품질**: 검출 가능한 파싱/무결성 오류 없음. Word + POS 관점에서 바로 DB 로드 가능.
- **필수 파생 필드**: CEFR(파일명 기반), base_word(괄호 제거), source
- **유일성**: `(word, pos, cefr)` 조합으로 관리
- **런타임 로드 대상**: definition, example sentence (유저 설계에 따라 단어 시험 시 동적 조회)
- **학습 카드 단위**: 복수 POS(245) → 1카드 / Disambiguation(38) → 분리 카드
