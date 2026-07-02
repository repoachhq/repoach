---
description: Dream Mode — deliberate reflective memory consolidation pass
---

You are in **Dream Mode** — a deliberate, reflective pass over this project's
durable memory, the way sleep organises memories in the brain. Synthesise what
has been learned recently into well-organised, current memories so future
sessions orient quickly.

This runs ONLY when you invoke `/dream` — never automatically. A SessionStart
hook may remind you when a dream is due, but it never triggers one.

## Live context

!`slug=$(echo "${CLAUDE_PROJECT_DIR:-$PWD}" | sed 's#[/_]#-#g'); mem="$HOME/.claude/projects/$slug/memory"; echo "memory_dir=$mem"; echo "memory_files=$(ls "$mem"/*.md 2>/dev/null | wc -l)"; echo "last_dream=$(python3 -c "import json; print(json.load(open('$mem/.dream_state.json')).get('last_dream','never'))" 2>/dev/null || echo never)"; cmd="${CLAUDE_PROJECT_DIR:-$PWD}/CLAUDE.md"; echo "claude_md_bytes=$(stat -c%s "$cmd" 2>/dev/null || echo 0) (budget 40000)"`

## Instructions

Run these BEFORE handling any other request in this turn.

1. Read ALL files in the memory directory above: `MEMORY.md` + every `*.md`.
2. Read `.dream_state.json` for context on previous dreams.
3. Analyse the corpus for:
   - **STALE** — outdated info (completed tasks still listed as open, resolved
     blockers, obsolete versions/dates). **Verify against current code/git
     before trusting a memory's own claim** about merge or ship state.
   - **CONTRADICTIONS** — two memories asserting conflicting things.
   - **REDUNDANCIES** — multiple memories on one topic — merge them.
   - **EMPTY** — files with no useful content.
4. For each issue: merge redundant topic files (delete the weaker, fold it into
   the stronger); update stale facts to current reality; remove resolved
   blockers/TODOs; keep the YAML frontmatter (`name`, `description`, `type`);
   **do NOT touch `feedback_*` memories — they are durable rules.**
5. Regenerate `MEMORY.md`: one line per topic file, grouped by section, under
   200 lines; drop entries for deleted files; add entries for merged/new files.
6. Update `.dream_state.json`: set `last_dream` to the current UTC ISO
   timestamp, `last_dream_session_count` to the total JSONL session count, and
   append to `dreams[]`: `{date, merged, pruned, updated, agents_updated,
   summary}`.
7. Agents (`.claude/commands/*.md`, `.claude/agents/*.md`) evolve too — but
   surgical edits ONLY: update obsolete phase/version numbers, flip "⏳ Not yet"
   → "✅ Complete", fix stale stack references (verified against actual code),
   add a one-line "Common debugging" entry for a freshly-resolved trap. NEVER
   touch "Your Role" / "Your Approach" / "When to invoke" sections (agent
   personality is stable by design). Record edits in `.dream_state.json` →
   `agents_updated`.
8. CLAUDE.md — budget 40000 bytes (current size shown above). If at or under
   budget, do not touch it. Otherwise prune it back under budget: condense the
   oldest status rows to one-liners and move verbose detail into a new
   `project_phase_archive_<date>.md` (referenced from MEMORY.md). Preserve
   structural headings, doc links, common commands, code style, git workflow,
   project structure, custom agents, and architecture sections. Record
   `claude_md: {before, after, archived_to}` in `.dream_state.json`.
9. Briefly report what you did, e.g. "Dream Mode: merged 2 memory files, pruned
   1 stale entry, updated 3 facts, refreshed 2 agents, CLAUDE.md untouched."
