---
name: Stack naming conventions
description: Actual Komodo stack names — many have server suffixes that the system prompt omits
type: reference
---

Komodo stack names often have a server suffix appended. The system prompt may use shorthand names that don't match the actual Komodo resource name.

Known mappings:
- `git-sammler` → actual name: `git-sammler-werkstatt` (on werkstatt-1)
- `git-fundgrube` → actual name: `git-fundgrube-werkstatt` (on werkstatt-1)
- `git-keycloak` → actual name: `git-keycloak` (on werkstatt-1)
- `litfasssaeule` → actual name: `litfasssaeule-werkstatt` (on werkstatt-1)
- `taktgeber` → actual name: `taktgeber-werkstatt` (on werkstatt-1)
- `docktail` → per-server stacks: `docktail-werkstatt-1`, `docktail-nebula-1`, etc.

**How to apply:** When a `km execute deploy-stack <name>` fails with EXECUTION FAILED, first run `km list` to see the actual registered stack name. The pattern is `<base-name>-<server-name>` for werkstatt-hosted stacks.
