# Adapter: hermes — Hermes Runtime (Security Domain Worker Pools)

- Maps AgentContext → Hermes ACP session
- Pool selection via control-plane/router.py (Section 16)
- High-risk → ephemeral sandbox
- Hermes core NOT modified — ACPAdapter is sole integration point
