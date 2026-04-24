---
name: vocabulary
description: Run a scheduled FSRS vocabulary review session from the root repository runtime.
allowed-tools: Bash, AskUserQuestion
---

## Setup

The system Python (3.9) is too old for fsrs 6.x. Always use `python3.11` for all scripts. fsrs 6.3.1 is already installed under Python 3.11.

If `data/vocabulary.db` is missing, or the `words` table is still empty, stop and tell the user to populate the DB in the separate build-db session first.

## Steps

1. Run `python3.11 scripts/get_daily_words.py` from the repository root and parse the JSON output.
2. If no words are due, inform the user and exit.
3. For each due word, use AskUserQuestion with exactly these fields:
   - question: "{word}  ·  {pos}"
   - header: "{current}/{total}{' NEW' if is_new else ''}" (keep under 12 chars, e.g. "3/20 NEW" or "3/20")
   - options:
     - label: "Again", description: "잘 기억나지 않음"
     - label: "Good", description: "기억함"
   - After the user selects, run `python3.11 scripts/record_study.py --word-id <id> --rating <again|good>`.

4. If `record_study.py` fails for any word:
   - Show the error briefly.
   - Use AskUserQuestion to ask whether to skip (`Skip`) or quit (`Quit`).
   - Do not continue automatically.
   - Do not commit or push after an error-intervened session.
5. After a clean session, show a short summary with reviewed count, `Again` count, and `Good` count.
6. Only if the user explicitly asks for git persistence, stage or commit `data/vocabulary.db`. Do not run `git commit` or `git push` automatically.
