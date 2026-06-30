# Changelog

## [0.3.16] - 2026-06-30

- chore(deps): security upgrades (pip-audit)


## [0.3.15] - 2026-06-30

- chore: hygiene + security pass


## [0.3.14] - 2026-06-10

- feat(apple-notes): defensive elicit confirmation on delete_note (shelf) (#21)


## [0.3.12] - 2026-06-10

- feat: fastmcp 3.4.2 uplift — annotations, structured output, resources, prompts, context (#20)


## [0.3.10] - 2026-05-04

- fix(threading,url): per-thread FTS connections + applenotes:// deep-link


## [0.3.9] - 2026-05-03

- fix(threading): use `threading.local()` for the FTS in-memory SQLite connection so each FastMCP thread-pool worker gets its own connection; eliminates `ProgrammingError: SQLite objects created in a thread can only be used in that same thread` on `search_notes` / `delete_note` and all tools that touched the FTS cache across thread hops
- fix(url): `create_note` and `create_recipe_note` now return `applenotes://showNote?identifier=<UUID>` instead of a bare `applenotes://` — identifier is resolved from NoteStore.sqlite with a 3-second retry window to accommodate Apple Notes' async WAL flush


## [0.3.8] - 2026-04-21

- fix(security): change host default from 0.0.0.0 to 127.0.0.1


## [0.3.7] - 2026-04-21

- fix(security): SecretStr for apple_notes_mcp_api_key


## [0.3.6] - 2026-04-20

- feat(tools)!: rename all tool names from kebab-case to snake_case


## [0.3.5] - 2026-04-20

- ci(deps): enable Dependabot weekly updates


## [0.3.4] - 2026-04-20

- chore(deps): refresh uv.lock after pre-commit regen


## [0.3.3] - 2026-04-20

- chore(pkg): add __main__.py and py.typed per MCP Server Standards


## [0.3.2] - 2026-04-18

- feat(reliability): stateless_http + /health + FastMCP 3.2.4


## [0.3.1] - 2026-04-12

- feat: v0.3.0 — read, search, and tag tools via NoteStore SQLite


## [0.2.0] - 2026-04-09

### Changed
- Bumped FastMCP dependency to >=3.2.2
- Added write-only server notice to instructions

### Added
- Automated version bump and release CI via GitHub Actions
- CHANGELOG.md for tracking changes
