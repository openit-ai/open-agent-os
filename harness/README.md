# Harness — agent configuration files

The files that shape your agent — identity, memory, project instructions — plus the wider surface around them: settings, secrets, skills, schedules. All plain files on your machine; portable, editable, and entirely yours.

## The configuration surface

| File | What it defines | Managed by | Loaded |
|---|---|---|---|
| `SOUL.md` | Identity, voice, working principles | **you** (the agent can draft) | every session — slot #1 |
| `memories/USER.md` | Your profile — preferences, style | **the agent** — saved automatically from your conversations; you can edit | every turn |
| `memories/MEMORY.md` | The agent's notes — environment, conventions, lessons | **the agent** — written and maintained automatically; you can correct | every turn |
| `AGENTS.md` | Project instructions, per workspace | **you** | while working in that project |
| `config.yaml` | Settings — model, providers, gateway, personalities | you / `hermes config set` | on start |
| `.env` | Secrets — keys and tokens | you / `hermes config set` (mode `600`) | on start |
| `skills/` | Procedures the agent can load | the agent + you | on demand |
| `cron/` | Scheduled jobs | the agent + you | scheduler |

Default profile locations:

```text
~/.hermes/SOUL.md
~/.hermes/memories/USER.md
~/.hermes/memories/MEMORY.md
~/.hermes/config.yaml
~/.hermes/.env
~/.hermes/skills/
~/.hermes/cron/
```

Project files (`AGENTS.md`) live in the project / workspace directory; Hermes also detects `.hermes.md` / `HERMES.md` for project instructions.

## Who writes what

- **`SOUL.md` is yours.** You define who the agent is and how it works; Hermes never overwrites an existing `SOUL.md`.
- **`USER.md` and `MEMORY.md` are agent-managed.** The agent saves what it learns automatically and maintains both files — you do not hand-write them. Read and correct them anytime; the agent treats your edits as the current state.
- **`AGENTS.md` is yours, per project.** Rule of thumb: *if it should follow you everywhere, it belongs in `SOUL.md`; if it belongs to a project, it belongs in `AGENTS.md`.*

## Principles (for the memory files)

1. **Declarative facts, not instructions.** "User prefers concise reports" ✓ — "Always answer concisely" ✗. Imperative phrasing gets re-read as a directive in later sessions and can override the user's current request.
2. **High signal only.** A character budget applies to each file; when it fills, consolidate or replace entries — never append endlessly.
3. **No secrets.** Ever. Keys, tokens, and passwords live in `.env` (mode `600`) — not in the harness.
4. **One fact per entry**, `§`-separated, written so a future session understands it with no extra context.
5. **Procedures go to skills; facts go here.** Reusable workflows belong in a `SKILL.md`; the harness is for who you are, what the environment is, and lessons worth keeping.

## Seeding (first run)

See [`seeding.md`](seeding.md) — a short conversation that produces your `SOUL.md` and a first `USER.md` draft; the agent pre-fills `MEMORY.md` with environment facts and maintains it from then on. The bootstrap skill runs this as part of the install phase.

## Maintenance

- Review monthly; prune stale facts. A fact that goes stale within a week does not belong here — that is session history.
- The agent edits memory itself; correct it when it drifts.
- When a file nears its budget, the agent reports it — consolidate then, don't wait for overflow.

## References

- Official docs: *Personality & SOUL.md* (`https://hermes-agent.nousresearch.com/docs/user-guide/features/personality`) and *Persistent Memory* (`https://hermes-agent.nousresearch.com/docs/user-guide/features/memory`).

## Templates

- [`templates/SOUL.md`](templates/SOUL.md) — persona & working principles starter (you fill this)
- [`templates/USER.md`](templates/USER.md) — profile starter (seeded with you; the agent maintains it)
- [`templates/MEMORY.md`](templates/MEMORY.md) — shape reference (agent-managed)
- [`templates/AGENTS.md`](templates/AGENTS.md) — project instructions starter

Each template carries fill-in markers and short guidance comments; delete the comments once filled.
