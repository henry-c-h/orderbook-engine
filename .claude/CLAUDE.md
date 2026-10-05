# CLAUDE.md

## What this is

Deterministic limit order book engine with an async, event-driven service around it. Python 3.13, managed with uv.

## Workflow

Spec changes: propose first, never change matching rules silently.
Tests come with every behaviour change.
Don't overclaim in README or docs.

## Commands

- `uv run pytest`
- `uv run ruff check . && uv run ruff format .`
- `uv run pyright`

Run all three before every commit.

## Testing

- Put a `# given`, `# when` and `# then` comment above each block of a test,
  so a test's purpose can be scanned at a glance. Leave out a block only if
  the test has nothing to put in it.

## Commits

- Only commit when asked. Never push, force-push or rewrite pushed history
  unless asked.
- Never bypass hooks (`--no-verify`). If pre-commit or the commit-msg check
  fails, fix the cause and commit again.
- Format: `type: subject` or `type(scope): subject`. Scope is optional; use
  it only when the change sits clearly inside one of: domain, engine, app,
  api, replay, ui.
- Types: feat (new behaviour), fix (bug fix), test (tests only),
  refactor (no behaviour change), perf, docs, build (deps, packaging),
  ci, chore (everything else).
- Subject: imperative, lowercase, no trailing period, max 50 chars.
  Optional body wrapped at 72 chars, explaining why, not what.
- One logical change per commit. Tests go in the same commit as the
  behaviour they cover. Every commit must pass pytest, ruff and pyright.
- Examples: `feat(engine): match limit orders by price-time priority`,
  `test(engine): add hypothesis invariant for crossed book`,
  `docs: add matching engine spec`.

## Tool use

- Prefer the Read, Grep and Glob tools over shell pipelines. Run simple
  commands one at a time; don't chain with `;`, `&&` or redirects.
