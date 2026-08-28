# Agent Runtime — §16.1

Minimal built-in LLM Runtime per §16.1 (BSL 1.1).

- `LLMProviderAdapter` — litellm wrapper with model routing, streaming, retry
- `StructuredToolLoop` — messages → tool_calls → `gateway.call` via `trace_id` → messages (max_steps, termination)
- Retry/timeout + Observability hook (audit log stub)

Zero-Bypass: all tool execution via Execution Gateway with `trace_id`; no direct shell/python.

Lazy `litellm` deps — falls back to mock when not installed. All functions typed, no shell.
