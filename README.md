# Girder

Personal AI development orchestrator: describe a feature in natural language,
get a validated spec, approve it, and let sandboxed agents implement, test,
verify, and deliver it to `main`.

- **Specification:** [`docs/plan.md`](docs/plan.md) (v2.2)
- **Engineering blueprint:** [`docs/implementation-plan.md`](docs/implementation-plan.md)

Status: Sprints 1–4 complete (infrastructure, spec engine, sequential
autonomous loop, GitHub delivery pipeline with tiered merge) — see the
implementation plan's delivery table.

## Development

```bash
uv sync          # install
uv run pytest    # unit + integration tests (integration skips without a runtime)
uv run ruff check .
uv run mypy
```
