# memory_system design review ideas

> 2026-07-09 Codex read-only design review, using the `codebase-design` vocabulary.
> This note records follow-up ideas, not completed changes.

## Overall judgement

The core shape is healthy. The strongest deep modules are:

- `fragments.py` + `archive.py` + `index.py`: the "fragments are source of truth, SQLite is rebuildable index" rule is clear, load-bearing, and well localized.
- `recall/episode.py` + `recall/ranking.py`: `recall_episode()` exposes a small interface while hiding vector/FTS recall, decay, ranking, slots, alias bridges, dedup, cooldown, and clock updates.
- `agent/registry.py`: provider knowledge has mostly been pulled into one place, which is the right direction.

The main risk is not the domain model. The main risk is that outer modules, especially `server.py`, are starting to absorb behavior that deserves deeper modules of its own. If more entry points arrive, especially MCP tools, the duplicated orchestration will become expensive.

## Priority 1: stop UUIDs from reaching the browser

### Observation

`ARCHITECTURE.md` says uuid / vectors should never reach the UI. `ui_shape.py` correctly strips `covered_uuids` from chunks and staging episodes, but `/api/transcript` still returns per-turn `uuids`:

- `memory_system/server.py:_api_transcript`

The current selection flow only needs `turn_idxs`; the server can map turns back to UUIDs internally. The browser does not appear to need raw UUIDs.

### Suggested change

Remove `uuids` from the `/api/transcript` response. Keep:

- `idx`
- `human_text`
- `assistant_text`
- `msg_count`
- `processed`

If some frontend code currently inspects `uuids`, replace that usage with `msg_count` or `processed`.

### Completion standard

- `/api/transcript` no longer returns `uuids`.
- Existing select/processed behavior still works.
- Add or update a web API regression so the transcript payload is checked for absence of UUID-like fields.

### Verification

- `.venv/bin/python scripts/verify_web_api.py`
- Any existing S1/S2 verification that covers transcript preview/selection.

## Priority 2: extract provider/config administration out of `server.py`

### Observation

`server.py` currently owns too much provider administration:

- custom provider id generation
- base URL validation and warnings
- `.env` writes
- `custom_providers.json` writes
- role override cleanup when a provider is deleted
- hot mutation of `cfg.agent`

That makes the HTTP layer a large, shallow module. The behavior is useful outside HTTP: CLI, MCP, or future maintenance scripts may all want the same semantics.

### Suggested seam

Create a deeper module such as `agent/settings.py` or `agent/provider_admin.py`.

Possible interface:

```python
def get_agent_settings(cfg: Config) -> AgentSettingsView: ...
def update_role_settings(cfg: Config, role: str, *, provider: str | None, model: str | None) -> UpdateReport: ...
def add_custom_provider(cfg: Config, name: str, base_url: str, model: str = "") -> ProviderReport: ...
def update_custom_provider(cfg: Config, provider_id: str, *, name: str, base_url: str, model: str) -> ProviderReport: ...
def remove_custom_provider(cfg: Config, provider_id: str) -> RemoveProviderReport: ...
```

The HTTP handler should validate transport shape, call this module, and serialize the report. The provider administration module should own the "why" of provider state changes.

### Completion standard

- Provider CRUD behavior is no longer implemented in `server.py`.
- `.env` and `custom_providers.json` update order is tested through the new module interface.
- `server.py` no longer needs to know how to generate custom provider ids or clear dangling role overrides.

### Verification

- `.venv/bin/python scripts/verify_provider_config.py`

## Priority 3: make runtime config mutability explicit

### Observation

`Config` and `AgentConfig` are frozen dataclasses, but `server.py` mutates `cfg.agent` with `object.__setattr__`.

This breaks the interface implied by `frozen=True`: callers cannot tell whether `Config` is an immutable startup snapshot or a runtime settings holder.

### Suggested change

Pick one of these two shapes:

1. Keep `Config` frozen as a startup snapshot, and introduce a separate runtime settings module/store used by the server.
2. Make mutability explicit with a small wrapper, for example `RuntimeConfig`, that owns `agent` hot updates under a lock.

Recommended direction: keep `Config` frozen and move hot state into the provider/config administration module from Priority 2. That preserves the current value of `Config` as a stable dependency object.

### Completion standard

- No `object.__setattr__(cfg, ...)` remains in production code.
- The interface tells callers whether they are reading startup config or current runtime provider settings.
- Provider config tests still prove same-page updates behave as expected.

### Verification

- `.venv/bin/python scripts/verify_provider_config.py`
- `rg "object\\.__setattr__" memory_system`

## Priority 4: distinguish missing work files from corrupt work files

### Observation

Both work-state stores return `None` for "file does not exist" and "file exists but failed to parse":

- `memory_system/segments_store.py:load`
- `memory_system/staging_store.py:load`

This is a friendly interface for callers, but it hides data damage. A corrupt JSON file can be treated as absent, and a later write may overwrite work that should have been preserved for recovery.

### Suggested change

Define a store-level error, for example:

```python
class StoreCorruptError(RuntimeError):
    path: Path
```

Then:

- missing file -> `None`
- corrupt JSON / unreadable file -> raise `StoreCorruptError`

HTTP should turn that into a visible 500/409-style response saying the work file is corrupt and should not be overwritten automatically.

### Completion standard

- Corrupt chunk/staging JSON is not silently treated as empty state.
- Save paths do not overwrite corrupt work files without explicit repair.
- A regression covers corrupt JSON for both stores.

### Verification

- New focused store tests or a small verification script.
- Existing web API verification still passes.

## Priority 5: delay, but prepare for, an ingest workflow module

### Observation

CLI and HTTP both orchestrate similar chunk/extract flows:

- path validation / transcript cleaning
- provider selection
- model selection
- agent invocation
- work-state persistence
- retry recording

Today this duplication is tolerable. It becomes a real cost if MCP or automation adds another ingest entry point.

### Suggested seam

When a third entry point appears, create an `ingest_workflow.py` module that owns chunk/extract orchestration behind transport-neutral interfaces.

Possible interface:

```python
def chunk_transcript(cfg: Config, path: Path, *, provider: str | None = None, model: str | None = None) -> ChunkWorkflowResult: ...
def extract_transcript_segments(cfg: Config, path: Path, *, seg_ids: list[str] | None = None, provider: str | None = None, model: str | None = None, max_workers: int = 1) -> ExtractWorkflowResult: ...
```

This should not absorb HTTP path confinement; that remains a transport concern. It should absorb provider/model selection, agent calls, retries, and store writes.

### Completion standard

- CLI and HTTP call the same workflow module for chunk/extract.
- Workflow tests exercise behavior through the workflow interface rather than through CLI and HTTP separately.
- Transport modules only translate inputs/outputs.

### Verification

- `.venv/bin/python scripts/verify_s3.py`
- `.venv/bin/python scripts/verify_s4.py`
- `.venv/bin/python scripts/verify_web_api.py`

## Priority 6: split the graph projection out of `views.py`

### Observation

`list_memories()` in `views.py` does a full projection on every request: it scans `episode_nodes`, counts labels, and rebuilds co-occurrence edges with an `O(E·K²)` pass. That is fine for a small corpus, but it makes the "查看" screen scale with total historical load, not with the current viewport or requested slice.

### Suggested seam

Create a deeper module such as `graph_projection.py` or `edges_store.py` that owns:

- node counts for the current active set
- co-occurrence edges
- optional cache/invalidation hooks for confirm/archive/delete

The HTTP/view layer should only ask for a ready projection, not reconstruct it every time.

### Completion standard

- `/api/memories` no longer rebuilds all edges from scratch on every request.
- Graph data can be refreshed incrementally or via an explicit cache rebuild.
- A regression covers a nontrivial corpus so the projection cost doesn't silently creep back.

### Verification

- `.venv/bin/python scripts/verify_view_api.py`
- A larger-corpus smoke test or benchmark if one gets added later.

## Priority 7: add one shared workflow test seam

### Observation

The repository has 12 `scripts/verify_*.py` scenario scripts, which is decent coverage, but the same lifecycle logic is spread across ad hoc drivers rather than a shared test boundary. That makes behavior harder to reuse when the next entry point arrives.

### Suggested seam

Keep the scenario scripts, but add a small shared fixture/helper layer for:

- transcript setup
- fake providers
- database reset
- common assertion helpers

If you want a deeper seam, a thin `tests/` package around the ingest/archive/recall workflows would be the next step.

### Completion standard

- The verify scripts share fixtures instead of each re-building the same scaffolding.
- Core ingest/archive/recall behaviors are exercised through reusable workflow helpers.
- Scenario scripts remain runnable as standalone entry points.

### Verification

- Existing `scripts/verify_*.py` runs stay green.
- A new shared helper test if/when that layer exists.

## Non-goals for now

- Do not split `recall_episode()` just because it is long. It is currently a deep module with a useful small interface. `ranking.py` is already the right kind of internal seam.
- Do not add a repository-wide abstraction layer for storage. `fragments.py`, `archive.py`, and `index.py` already express the important storage rule clearly.
- Do not refactor the frontend merely because some files are large. The more urgent design pressure is backend seam placement around provider settings and work-state safety.

## Recommended order

1. Remove UUIDs from `/api/transcript`.
2. Extract provider/config administration from `server.py`.
3. Eliminate `object.__setattr__` by making runtime settings explicit.
4. Make corrupt work-state files fail visibly.
5. Add `ingest_workflow.py` only when a third ingest entry point appears.
6. Split graph projection out of `views.py` once the view graph becomes a performance concern.
7. Add shared workflow test helpers while keeping the standalone verify scripts.
