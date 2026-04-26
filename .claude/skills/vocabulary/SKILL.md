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
     - label: "Again", description: ""
     - label: "Good", description: ""
     - label: "Stop", description: ""
   - If "Again" or "Good": store `{"word_id": <id>, "rating": "<again|good>"}` in memory. Do NOT call any script.
   - If "Stop":
     - If 0 ratings have been collected so far: exit immediately.
     - Otherwise: use AskUserQuestion — question: "Save progress?", header: "Stop",
       options: [{label: "Yes", description: "Save and exit"}, {label: "No", description: "Exit without saving"}]
       - Yes: pipe the collected ratings to batch_record_study.py (step 3b), show summary (step 5), then exit.
       - No: exit immediately with no DB changes.

3b. After all words have been rated, pipe the collected JSON array to `batch_record_study.py` via stdin:
    ```
    echo '<json_array>' | python3.11 scripts/batch_record_study.py
    ```
    Example: `echo '[{"word_id":1,"rating":"good"},{"word_id":2,"rating":"again"}]' | python3.11 scripts/batch_record_study.py`

4. If `batch_record_study.py` exits non-zero:
   - Show the error output (includes which word_id caused the failure).
   - Use AskUserQuestion to ask whether to retry (`Retry`, re-runs the full batch with ratings still in memory) or quit (`Quit`).
   - Do not continue automatically.
   - Do not commit or push after an error-intervened session.
5. After a clean session, show a short summary with reviewed count, `Again` count, and `Good` count.
6. Only if the user explicitly asks for git persistence, stage or commit `data/vocabulary.db`. Do not run `git commit` or `git push` automatically.
