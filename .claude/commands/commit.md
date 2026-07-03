---
description: Create a clean git commit following project conventions
---

Create a clean git commit for the current staged + tracked-modified state.

## Workflow

1. Run in parallel:
   - `git status` (no `-uall` — large repos blow up)
   - `git diff` (staged + unstaged)
   - `git log -10 --oneline` to learn the project's commit style

2. Analyse the changes:
   - What's the *behaviour* change in plain English? (not the file list)
   - Single logical change, or several?
   - Any files that should NOT be committed (`.env`, `credentials.json`, large binaries)?

3. Draft message:
   - Conventional Commits style if the project uses it (`feat(scope): …`, `fix: …`)
   - First line ≤ 72 chars, imperative mood
   - Body explains *why*, not *what* (the diff already shows what)
   - No sprint codes, US numbers, week numbers, or ticket IDs in the message body —
     those belong in the PR description, not the git log.

4. Stage explicit files (`git add path/to/file`), never `git add -A` or `git add .`
   (those grab `.env` and friends).

5. Commit via heredoc:
   ```
   git commit -m "$(cat <<'EOF'
   feat(scope): one-line summary

   Body paragraph explaining why this change exists.
   EOF
   )"
   ```

6. Confirm with `git status`.

## Constraints

- NEVER include `Co-Authored-By` lines unless explicitly requested.
- NEVER use `--no-verify` — pre-commit hooks exist for a reason.
- If a hook fails, fix the underlying issue and create a NEW commit (not amend).
- Don't push without being asked.
