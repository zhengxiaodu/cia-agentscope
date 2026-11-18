# Historical File Restore Implementation Plan

> For agentic workers: use superpowers:executing-plans to execute the existing approved on-demand restoration approach.

**Goal:** Recover a persisted historical artifact into the current session sandbox only when requested.

**Architecture:** Session-scoped history service validates ownership and metadata before reading an existing shared backup. Agent tools list and restore files through the request's sandbox adapter. Atomic non-overwriting publication and request-local signatures preserve current edits and original message ownership.

**Tech Stack:** Python, asyncio, AgentScope FunctionTool, OpenSandbox SDK, existing MySQL session_files.

**Spec:** ../specs/2026-11-05-history-file-restore-design.md

## Global constraints

- Keep charts and configuration unchanged; no schema migration or new dependency.
- Preserve binary bytes, session isolation, existing work files and user changes.
- Use shared SESSION_FILES_PERSIST_DIR for cross-replica access; do not fetch arbitrary URLs.

## Review focus

- Traversal and symlink escapes cannot read another session's backup.
- Metadata lookup errors cannot masquerade as an empty history.
- Restore races cannot overwrite current sandbox files.
- Restored files cannot be rebound to a new message unless changed.
- Actual sandbox working directory must match the advertised session directory.

## Tasks

- [x] Add failing restoration tests, then implement HistoricalFileService and ListHistoryFiles / RestoreHistoryFile tools.
- [x] Add failing binary adapter tests, then implement atomic restore and SDK working_directory / timeout forwarding.
- [x] Wire session service and RoundState into workspace assembly; add history-tool instructions and restored-file signature handling.
- [x] Verify fresh-sandbox PPTX restoration, ownership, traversal, duplicate names, missing backup, repeat restore, cross-replica shared backup, restored-file detection and existing regressions.
- [x] Review the final diff and copy only guarded changed files back to the requested repository.

## Execution ledger

Ruling: use an isolated source snapshot in the allowed artifact directory because the repository has moved outside the writable roots. Preserve source hashes and verify them before writing back; no branch changes or commits are required by this task.

Ruling: re-use the existing unique (session_id, path) identity. Historical versions already overwritten at the same path are outside recoverable stored data; list message_pair_id and timestamps for existing records without inventing version IDs.

Task 1: complete — service RED missing module, then 18 service tests GREEN. Task 2: complete — adapter RED missing methods/workdir forwarding, then combined 30 tests GREEN. Task 3: integration RED 3 failures; now wired with session service and request-local restored signatures. Adjacent regression found two baseline mismatches (SQL row-alias upsert vs old VALUES assertions; existing JSON whitelist vs outdated exclusion), verifying against untouched source.

Final staging validation: 109 targeted tests passed. All changed Python files compile. Untouched-source checks reproduced the stale SQL/JSON expectations and five input-probe fixture failures; only the affected tests were adapted, with no changes to production probe/SQL/whitelist behavior.

Independent code review: corrected OpenSandbox file mode to SDK decimal 600, retained literal-percent filenames, and prevented local storage exceptions from leaking host paths. Reviewer found no remaining actionable P0/P1/P2 findings in the final feature diff. Live S1, MySQL, Redis and OpenSandbox integration are pending; local tests mock those boundaries.

Guarded writeback completed: source hashes matched the reviewed baseline; copied only the 13 Python changes and these two design/plan documents. No charts or configuration changes.
