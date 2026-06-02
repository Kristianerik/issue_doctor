# Skill Template — issue_doctor

Copy this file to `skills/user/your-skill-name.md` and fill it in.
The filename becomes the skill name. Delete this header comment block when done.

Skills are injected into the model's context before diagnosis. Write them
as if you're briefing a smart engineer who is new to this codebase — give
them the map they need to navigate quickly.

Tips for writing effective skills:
- Be specific: name actual files, functions, and data structures
- Include "gotchas" — things that look fine but aren't
- Add the heuristics experienced engineers carry in their heads
- Keep trigger keywords accurate — they drive auto-detection

---

# Skill: [Your Skill Name]

## Trigger keywords
<!-- Comma-separated words/phrases. If any appear in the issue title, body,
     or labels, this skill is automatically loaded. Be specific enough to
     avoid false positives. -->
keyword1, keyword2, keyword3

## Overview
<!-- One paragraph: what is this subsystem/domain, what kinds of bugs
     appear here, what makes them tricky. -->

## Key source locations
<!-- List the files, directories, or modules that are most relevant.
     Include a short note on what each one does. -->
- `path/to/file.c`   — what it does
- `path/to/module/`  — what it does

## Common bug patterns

### Pattern name
<!-- Describe the pattern, why it happens, how to recognise it. -->

### Another pattern
<!-- ... -->

## Investigation steps
<!-- Concrete, ordered actions. Not "check the logs" — name the exact
     flag, function, or tool and what to look for. -->
1. ...
2. ...

## Useful commands / debug flags
```bash
# example command with explanation
```

## Subject matter experts
<!-- GitHub handles of people who know this area well, if known. -->
- `@handle` — area of expertise

## Key test locations
<!-- Where are the relevant tests? What test framework? -->
- `tests/path/` — what they cover
