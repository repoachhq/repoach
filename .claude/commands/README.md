# `.claude/commands/` — slash-command agents

Each `*.md` file in this directory is invokable as `/<filename-without-extension>`.

The four agents shipped with the bootstrap are role-agnostic:

| File | Slash command | Purpose |
|---|---|---|
| `architect.md` | `/architect` | System design, documentation, planning |
| `code-reviewer.md` | `/code-reviewer` | Quality, security, performance review |
| `commit.md` | `/commit` | Clean commits following project conventions |
| `feature-dev.md` | `/feature-dev` | 7-phase feature development workflow |

## Adding domain experts

As your project's domain crystallises, add experts here. Format:

```markdown
---
description: One-line summary shown in slash-command picker
---

You are now acting as the **{Role}** for this project.

## Your Role
...

## Your Approach
...

## When to invoke me
...
```

Keep "Your Role / Approach / When to invoke" stable — Dream Mode treats them
as personality and won't auto-edit them. Add a "Common debugging" or "Current
Status" section that Dream Mode *can* refresh as the project state evolves.
