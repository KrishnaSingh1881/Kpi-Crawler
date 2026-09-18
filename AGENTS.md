# Engineering rules

- Read the relevant requirements and existing implementation before editing.
- Keep the architecture simple, explicit, and limited to the current phase.
- Do not copy or refactor the legacy `university_kpi` repository.
- Do not implement future crawler, relevance, scoring, or intelligence features.
- Do not add abstractions without a current use.
- Preserve clear contracts between configuration, database access, migrations, and CLI code.
- Expected operational failures must be logged without exposing a traceback to CLI users.
- New behavior must have a focused automated check.
- Run tests and the applicable startup/database checks before handoff.
- Record completed work and unresolved issues in `progress.md`.
