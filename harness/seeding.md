# Harness Seeding — the first-run conversation

Set up the agent's identity and memory in about ten minutes. You (the agent) draft; the user confirms. After seeding, `MEMORY.md` keeps updating itself as you work — the user does not hand-write it. The bootstrap skill runs this during the install phase; it can be re-run any time the user says "update your profile / memory".

## Ownership (state this to the user)

- `SOUL.md` — the user's file; you may draft it, they own it.
- `USER.md` — you maintain it from conversations; the user can edit.
- `MEMORY.md` — you maintain it automatically; the user reviews and corrects.
- `AGENTS.md` — the user's, per project (only when they have project workspaces).

## Inputs

- Starting points: `harness/templates/` (SOUL.md, USER.md, MEMORY.md, AGENTS.md).
- Ten minutes with the user.
- Write targets: `~/.hermes/SOUL.md`, `~/.hermes/memories/USER.md`, `~/.hermes/memories/MEMORY.md`.

## The conversation (agent script)

Ask in small batches — this is a conversation, not an interrogation.

1. **Identity** (`SOUL.md`) — What should I be called? What is my role in one line? How should I address you?
2. **Communication** (`SOUL.md`) — Language, tone, length, formatting; any hard "never" rules (e.g. no filler, no emoji).
3. **Working style** (`SOUL.md`) — How much verification do you expect before I answer? When must I ask before acting (approval stance)? Cost sensitivity — free-first, or is a paid path fine when justified?
4. **About you** (`USER.md`) — Name and role; main channels; conventions (naming, reporting formats); context worth always knowing.
5. **Environment** (`MEMORY.md`, you write these) — Host OS; where projects and documents live; services that run; recurring jobs. No secrets.
6. **Draft & review** — Fill the templates, show the SOUL + USER drafts, take corrections, write the files. Show the MEMORY pre-fill for a quick correctness pass.
7. **Confirm** — Read back the paths with a one-line summary each; explain ownership (SOUL = theirs; USER/MEMORY = yours, correctable anytime); note that memory grows as you work together.

## Quality rules (bake into every draft)

- Declarative facts, not instructions: "User prefers X" ✓ — "Always do X" ✗.
- No secrets, no third-party personal data.
- One fact per entry, `§`-separated, compact — these files are read before every answer.
- Procedures belong in skills; only durable facts belong here.
- When the user is unsure, propose a sensible default and let them veto: "I'll start with this — correct me anytime."

## Re-seeding & maintenance

- "Update your profile / memory" → you update `USER.md` / `MEMORY.md`; touch `SOUL.md` only on the user's instruction.
- Monthly: prune stale facts; consolidate when a file nears its budget.
- The agent updates `MEMORY.md` as it works — treat those edits as proposals the user can correct.
