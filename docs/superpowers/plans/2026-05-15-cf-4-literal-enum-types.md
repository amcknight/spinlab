# CF-4: Literal/Enum types end-to-end — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace bare `str` / `list[dict]` / duplicated `Literal`-vs-`StrEnum` surfaces in the API and protocol layer with single-source enum types that flow through the OpenAPI codegen pipeline into `frontend/src/api-types.ts` as TS string unions.

**Architecture:** `python/spinlab/api_schemas.py` becomes the OpenAPI source of truth. `python/spinlab/models.py` owns the canonical Python enums (`Mode`, `EndpointType`, `Status`); `api_schemas.py` imports them rather than re-declaring `Literal[...]` siblings. The shared `Estimate`/`ModelOutput` value objects move to `pydantic.dataclasses.dataclass` so they have one definition usable from both internal Python code (`.to_dict()` / construction by estimators) and the FastAPI/OpenAPI schema. Protocol-layer event fields (`LevelExitEvent.goal`, `CheckpointEvent.cp_type`) and the `SetConditionsCmd.definitions` payload become typed (Literal, ConditionSpec dataclass) so pyright catches typos at construction sites.

**Tech Stack:** Python 3.11+, Pydantic v2 (`pydantic.dataclasses.dataclass`, `BaseModel` with `extra="allow"`), FastAPI OpenAPI auto-generation, `scripts/dump_openapi.py`, `openapi-typescript`, pytest with `--strict-markers` (just landed), pyright, ruff.

**Branch:** Continues on `improve/literal-enum-types-and-cleanups` (already has 4 trivial commits from earlier in the /improve session: `e9bbe01`, `21d97cf`, `d407dd2`, `dd0ba10`).

**Pre-flight (one-time, before Task 1):**

- [ ] Confirm fast suite + emulator suite are green on this branch (baseline)
- [ ] Confirm pyright clean on `python/`
- [ ] Confirm `cd frontend && npm run gen-types && npm test` is green
- [ ] Capture the current `frontend/src/api-types.ts` (this file is gitignored — eyeball `Mode`, `EndpointType`, `ActionResponse`, `ModelOutput` definitions so you can compare after each task)

Commands:
```bash
python -m pytest -q --no-header
npx pyright python/
cd frontend && npm run gen-types && npm test && cd ..
```

Expected: 892 passed, 0 pyright errors/warnings, frontend tests green.

---

## File Structure

Files this plan touches:

| File | Role |
|------|------|
| `python/spinlab/models.py` | Canonical `Mode`, `EndpointType`, `Status` enums. `Estimate` + `ModelOutput` become Pydantic dataclasses (Task 3). |
| `python/spinlab/api_schemas.py` | Imports enums from `models.py`; drops `Literal[...]` siblings. Imports `Estimate`/`ModelOutput` from `models.py`. `ActionResponse.status` re-typed to `Status` (Task 2). |
| `python/spinlab/protocol.py` | Adds `LevelExitGoal` + `CheckpointType` Literal aliases. `LevelExitEvent.goal` and `CheckpointEvent.cp_type` re-typed. Adds `ConditionSpec` dataclass for `SetConditionsCmd.definitions` (Task 5). |
| `python/spinlab/condition_registry.py` | `replace_with_read_specs(specs: list[ConditionSpec])` (Task 5). |
| `python/spinlab/session_manager.py` | `install_condition_registry` constructs `ConditionSpec` objects instead of bare dicts (Task 5). |
| `python/spinlab/retroarch/orchestrator.py` | `_on_set_conditions` passes typed specs through (Task 5). |
| `tests/unit/retroarch/test_conditions.py` | Updates `replace_with_read_specs` call sites to pass `ConditionSpec` objects (Task 5). |
| `tests/unit/retroarch/test_orchestrator.py` | Same (Task 5). |
| `tests/unit/retroarch/test_poller_conditions.py` | Same (Task 5). |
| `tests/unit/test_capture/*` and other tests using `LevelExitEvent(goal=...)` with non-canonical values | Replace `goal="goal"`, `goal="exit"`, `goal="boss"`, etc. with valid Literal values (Task 4). |

---

## Task 1: Single-source `Mode` and `EndpointType` (Cluster G dedup, part 1)

**Goal:** `api_schemas.Mode` and `api_schemas.EndpointType` import the enum classes from `models.py` instead of re-declaring `Literal[...]`. Pydantic v2 accepts Python `Enum`/`StrEnum` directly and serializes via `.value`; the OpenAPI schema emits a string enum; openapi-typescript emits a TS string-union.

**Files:**
- Modify: `python/spinlab/api_schemas.py` (lines 36-41 — drop Literal aliases, import from models)

### Steps

- [ ] **Step 1: Confirm `models.Mode` is compatible with Pydantic**

`models.py:12` defines `class Mode(Enum)`. Pydantic v2 accepts `Enum` and serializes `Mode.IDLE` to `"idle"` (the `.value`). It also accepts the string `"idle"` as input. The OpenAPI representation is `{"type": "string", "enum": [...values...]}`, identical to what `Literal[...]` produces.

No change needed in `models.py` yet — just verify the existing enum has all 7 members that match the Literal in `api_schemas.py:37-39`.

Read both lists (`Mode` values vs the Literal members). They should be 1:1.

- [ ] **Step 2: Replace the Literal definitions in `api_schemas.py`**

In `python/spinlab/api_schemas.py`, replace lines 36-41:

```python
Mode = Literal[
    "idle", "reference", "practice", "replay",
    "fill_gap", "cold_fill", "speed_run",
]

EndpointType = Literal["entrance", "checkpoint", "goal"]
```

with:

```python
# Re-export from models.py so there is a single source of truth. Pydantic v2
# treats Enum / StrEnum as a value type whose OpenAPI schema is a string enum,
# which openapi-typescript emits as a TS string union — identical wire format
# to the prior Literal alias, but with no second definition to drift.
from spinlab.models import EndpointType, Mode
```

The `Literal` import on line 22 stays — it's still used by `CaptureRunStatus`, `CaptureRunKind`, etc.

- [ ] **Step 3: Run pytest fast suite**

Run: `python -m pytest -m "not emulator" -q --no-header`
Expected: 880 passed (no regressions).

- [ ] **Step 4: Run pyright**

Run: `npx pyright python/`
Expected: 0 errors, 0 warnings, 0 informations.

If pyright complains about `Mode` no longer being a Literal at call sites, those call sites were probably using `Mode` as a Literal type alias. The change should be transparent — `Mode.IDLE` (the Enum member) and `"idle"` (the string) both validate against the new type. Investigate any failures before patching call sites.

- [ ] **Step 5: Regenerate frontend types and run frontend tests**

Run:
```bash
cd frontend
npm run gen-types
npm test
cd ..
```

Expected: frontend tests green; `frontend/src/api-types.ts` `Mode` and `EndpointType` definitions still produce TS string unions (not raw `string`).

Quick spot check after gen-types:
```bash
grep -A 1 "Mode" frontend/src/api-types.ts | head -20
```
You should see something like `Mode: "idle" | "reference" | "practice" | "replay" | "fill_gap" | "cold_fill" | "speed_run"`. If you see `Mode: string`, the codegen broke — STOP and investigate.

- [ ] **Step 6: Run full pytest (including emulator)**

Run: `python -m pytest -q --no-header`
Expected: 892 passed.

If any emulator test fails because of enum vs string comparison, check whether the test was constructing a `Mode` from a hard-coded string. The fix is to use `Mode("idle")` or `Mode.IDLE` — both work; the bare string also still works because Pydantic accepts it as input.

- [ ] **Step 7: Commit**

```bash
git add python/spinlab/api_schemas.py
git commit -m "$(cat <<'EOF'
api_schemas: single-source Mode + EndpointType from models.py

Both were defined twice — as StrEnum/Enum in models.py and as
Literal[...] in api_schemas.py. Re-export the canonical classes from
models.py; Pydantic v2 serializes Enum to .value, OpenAPI emits a string
enum, openapi-typescript emits the same TS string-union as before. One
definition, no drift.
EOF
)"
```

---

## Task 2: Tighten `ActionResponse.status` to `Status` enum (L5-Y1, L5-Y5)

**Goal:** `ActionResponse.status` and `OkResponse.status` and `ShutdownResponse.status` and `EmulatorLaunchResponse.status` are all currently `str`. The actual values returned by handlers come from `models.Status` (which has values `"ok"`, `"started"`, `"stopped"`, `"no_gaps"`). Pydantic v2 accepts a `StrEnum` directly; openapi-typescript will emit a TS string-union of the canonical values.

**Files:**
- Modify: `python/spinlab/api_schemas.py:153,158,420,424` (four `status: str` sites)

### Steps

- [ ] **Step 1: Audit what each `status: str` site actually returns**

Read these handler call sites to confirm each one returns a `Status` member (not some other string):

```bash
grep -n "status.*=.*\(Status\|\"ok\"\|\"started\"\|\"stopped\"\|\"no_gaps\"\)" python/spinlab/routes/ | head -40
```

Confirmed cases (from the merged scan + this prep):
- `ActionResponse.status` (line 153): set from `ActionResult.to_response()['status']` which is `self.status.value` (always a `Status` member).
- `OkResponse.status` (line 158): returned from misc routes as `"ok"`.
- `EmulatorLaunchResponse.status` (line 420): returned from `routes/system.py` as `"started"` or `"already_running"`.
- `ShutdownResponse.status` (line 424): returned as `"shutting_down"`.

WAIT — `EmulatorLaunchResponse` and `ShutdownResponse` return values that aren't in `Status` (`"already_running"`, `"shutting_down"`). **Do NOT tighten those two.** Only tighten `ActionResponse` and `OkResponse` where the value is genuinely a `Status` member.

- [ ] **Step 2: Replace the type annotations**

In `python/spinlab/api_schemas.py`, find the `ActionResponse` class around line 150:

```python
class ActionResponse(_BaseResponse):
    """Generic response for action endpoints. ``status`` is the outcome
    enum; ``session_id`` is set only when an action started a session."""
    status: str
    session_id: str | None = None


class OkResponse(_BaseResponse):
    status: str
```

Replace with:

```python
from spinlab.models import Status

class ActionResponse(_BaseResponse):
    """Generic response for action endpoints. ``status`` is the outcome
    enum; ``session_id`` is set only when an action started a session."""
    status: Status
    session_id: str | None = None


class OkResponse(_BaseResponse):
    status: Status
```

(Place the `from spinlab.models import Status` near the top of the file, next to the existing `from spinlab.models import EndpointType, Mode` added in Task 1 — merge into a single import.)

Leave `EmulatorLaunchResponse.status: str` and `ShutdownResponse.status: str` untouched.

- [ ] **Step 3: Run fast suite + pyright**

Run:
```bash
python -m pytest -m "not emulator" -q --no-header
npx pyright python/
```
Expected: 880 passed; pyright clean.

Note: `ActionResult.to_response()` returns `{"status": self.status.value}` — a bare string. When this dict flows into FastAPI's response building, Pydantic validates the string against the `Status` enum. Pydantic v2 accepts the string form of a `StrEnum`, so this still works without changing `ActionResult.to_response()`.

- [ ] **Step 4: Regenerate frontend types and verify**

```bash
cd frontend && npm run gen-types && cd ..
grep -B 1 -A 4 "ActionResponse" frontend/src/api-types.ts | head -20
```

Expected: the `status` field's TS type should now be `"ok" | "started" | "stopped" | "no_gaps"`, not `string`. If still `string`, codegen didn't pick up the enum — check that the import is at module top level (not inside a function).

- [ ] **Step 5: Run frontend tests**

```bash
cd frontend && npm test && cd ..
```

Expected: green. If any test asserted on `ActionResponse.status` with a string not in the Status enum, it needs updating. The narrowed type is the point.

- [ ] **Step 6: Run full pytest**

Run: `python -m pytest -q --no-header`
Expected: 892 passed.

- [ ] **Step 7: Commit**

```bash
git add python/spinlab/api_schemas.py
git commit -m "$(cat <<'EOF'
api_schemas: ActionResponse.status uses Status enum, not bare str

ActionResult.to_response() always emits a Status member's .value (ok /
started / stopped / no_gaps), and OkResponse routes return "ok" — but
the schema only knew the field was `str`, defeating the codegen pipeline:
openapi-typescript emitted `status: string` on the frontend. Tightening to
`status: Status` propagates a real TS string-union. EmulatorLaunchResponse
and ShutdownResponse stay `str` because they emit values outside the Status
enum ("already_running", "shutting_down").
EOF
)"
```

---

## Task 3: Single-source `Estimate` and `ModelOutput` via Pydantic dataclass (L1-A10)

**Goal:** `models.Estimate` and `models.ModelOutput` are plain dataclasses with `to_dict()` / `from_dict()` methods used throughout the Python codebase. `api_schemas.Estimate` and `api_schemas.ModelOutput` are Pydantic models with the same shape. Today they're kept in sync by convention. Convert the `models.py` versions to `pydantic.dataclasses.dataclass` so they have one definition, validation kicks in for free, and `api_schemas.py` imports rather than re-declares.

**Why this works:** `pydantic.dataclasses.dataclass` produces an object that IS a `dataclass` (so `dataclasses.asdict`, `dataclasses.fields`, and explicit instance methods like `to_dict()` all continue to work) AND is a Pydantic schema source (so FastAPI generates an OpenAPI definition from it). Existing call sites — `ModelOutput(total=..., clean=...)`, `output.to_dict()`, `ModelOutput.from_dict(d)` — keep working unchanged.

**Files:**
- Modify: `python/spinlab/models.py:199-239` (replace `@dataclass` with `@pydantic.dataclasses.dataclass` on `Estimate` and `ModelOutput`)
- Modify: `python/spinlab/api_schemas.py:51-59` (drop the two Pydantic classes; import from models)

### Steps

- [ ] **Step 1: Convert `models.Estimate` and `models.ModelOutput` to Pydantic dataclasses**

In `python/spinlab/models.py`, at the top:

```python
import dataclasses
import hashlib
import json
from dataclasses import dataclass, field
```

Add a Pydantic dataclass import. Keep the existing `from dataclasses import dataclass, field` — only the two classes that need OpenAPI schema emission switch. So add:

```python
from pydantic.dataclasses import dataclass as pydantic_dataclass
```

Then around line 199, replace:

```python
@dataclass
class Estimate:
    """One coherent set of predictions for a single time series."""
    expected_ms: float | None = None
    ms_per_attempt: float | None = None
    floor_ms: float | None = None

    def to_dict(self) -> dict:
        return {
            "expected_ms": self.expected_ms,
            "ms_per_attempt": self.ms_per_attempt,
            "floor_ms": self.floor_ms,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Estimate":
        return cls(
            expected_ms=d.get("expected_ms"),
            ms_per_attempt=d.get("ms_per_attempt"),
            floor_ms=d.get("floor_ms"),
        )


@dataclass
class ModelOutput:
    """What every estimator produces — predictions for total time and clean tail."""
    total: Estimate
    clean: Estimate

    def to_dict(self) -> dict:
        return {
            "total": self.total.to_dict(),
            "clean": self.clean.to_dict(),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ModelOutput":
        return cls(
            total=Estimate.from_dict(d["total"]),
            clean=Estimate.from_dict(d["clean"]),
        )
```

with:

```python
@pydantic_dataclass
class Estimate:
    """One coherent set of predictions for a single time series.

    Pydantic dataclass: behaves as a plain @dataclass at runtime (asdict,
    fields, ``to_dict``/``from_dict`` all work) AND emits an OpenAPI schema
    so api_schemas.py can re-export it rather than re-declaring.
    """
    expected_ms: float | None = None
    ms_per_attempt: float | None = None
    floor_ms: float | None = None

    def to_dict(self) -> dict:
        return {
            "expected_ms": self.expected_ms,
            "ms_per_attempt": self.ms_per_attempt,
            "floor_ms": self.floor_ms,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Estimate":
        return cls(
            expected_ms=d.get("expected_ms"),
            ms_per_attempt=d.get("ms_per_attempt"),
            floor_ms=d.get("floor_ms"),
        )


@pydantic_dataclass
class ModelOutput:
    """What every estimator produces — predictions for total time and clean tail.

    Pydantic dataclass: see ``Estimate`` for rationale.
    """
    total: Estimate
    clean: Estimate

    def to_dict(self) -> dict:
        return {
            "total": self.total.to_dict(),
            "clean": self.clean.to_dict(),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ModelOutput":
        return cls(
            total=Estimate.from_dict(d["total"]),
            clean=Estimate.from_dict(d["clean"]),
        )
```

- [ ] **Step 2: Replace `api_schemas.Estimate` and `api_schemas.ModelOutput` with re-exports**

In `python/spinlab/api_schemas.py`, replace lines 51-59:

```python
class Estimate(_BaseResponse):
    expected_ms: float | None = None
    ms_per_attempt: float | None = None
    floor_ms: float | None = None


class ModelOutput(_BaseResponse):
    total: Estimate
    clean: Estimate
```

with:

```python
# Single source of truth lives in models.py as a pydantic.dataclasses.dataclass.
# Imported here so the OpenAPI schema is generated from the same class the
# estimator pipeline constructs and ``state_builder`` serializes via to_dict.
from spinlab.models import Estimate, ModelOutput
```

(Merge with the imports added in Tasks 1 and 2 — one consolidated `from spinlab.models import ...` line.)

- [ ] **Step 3: Run fast pytest**

Run: `python -m pytest -m "not emulator" -q --no-header`
Expected: 880 passed.

Likely failure modes if anything goes wrong:
- `ModelOutput(total=..., clean=...)` raises if you pass plain dicts instead of `Estimate` instances. The codebase already constructs from `Estimate` everywhere — search to confirm: `grep -rn "ModelOutput(" python/spinlab/estimators/` — every result should pass `Estimate(...)`, not a dict.
- `from_dict` calls need `d["total"]` and `d["clean"]` to be dicts — same as before, so unchanged.

- [ ] **Step 4: Run pyright**

Run: `npx pyright python/`
Expected: 0 errors.

- [ ] **Step 5: Regenerate frontend types and verify the shape is preserved**

```bash
cd frontend && npm run gen-types && cd ..
grep -B 1 -A 5 "ModelOutput" frontend/src/api-types.ts | head -20
grep -B 1 -A 5 "Estimate" frontend/src/api-types.ts | head -20
```

Expected: TS types for `ModelOutput` and `Estimate` are unchanged from the baseline — fields and nullability should match what they were before this task.

- [ ] **Step 6: Run frontend tests and full pytest**

```bash
cd frontend && npm test && cd ..
python -m pytest -q --no-header
```

Expected: all green (892 backend + frontend tests).

- [ ] **Step 7: Commit**

```bash
git add python/spinlab/models.py python/spinlab/api_schemas.py
git commit -m "$(cat <<'EOF'
models+api_schemas: single ModelOutput/Estimate via pydantic.dataclasses

Estimate and ModelOutput existed as plain dataclasses in models.py (with
to_dict/from_dict, used by every estimator + the scheduler + state_builder)
AND as Pydantic models in api_schemas.py (for OpenAPI). They were kept in
sync by convention — if a field changed in one, the other silently drifted.

Switching the models.py definitions to pydantic.dataclasses.dataclass gives
us one definition that's both a runtime dataclass (asdict/fields/explicit
to_dict still work) and a Pydantic schema source. api_schemas.py now
imports them.
EOF
)"
```

---

## Task 4: Typed `goal` and `cp_type` on protocol events (L5-Y3, L5-Y4 partial)

**Goal:** Replace `LevelExitEvent.goal: str = "abort"` with a `Literal` type whose values match what `predicates.goal_type()` actually emits, and replace `CheckpointEvent.cp_type: str = ""` with a `Literal` whose values match what `predicates.check_checkpoint_hit()` emits. Update tests that pass non-canonical values.

**Verified values (from `python/spinlab/retroarch/predicates.py:30-45`):**
- `goal_type()` returns one of: `"key"`, `"orb"`, `"boss"`, `"normal"`, `"abort"`.
- `check_checkpoint_hit()` returns one of: `"midway"`, `"cp_entrance"`.

**Note:** These are protocol-layer `@dataclass(frozen=True)` — NOT Pydantic models, NOT in the OpenAPI surface. The Literal tightens pyright-side type-checking inside Python; frontend types are unaffected by this task.

**Files:**
- Modify: `python/spinlab/protocol.py:40, 65`
- Modify tests that pass non-canonical values to `LevelExitEvent(goal=...)` or `CheckpointEvent(cp_type=...)`.

### Steps

- [ ] **Step 1: Inventory test sites that need updating**

Find every test call site:
```bash
grep -rn 'LevelExitEvent(' tests/ python/spinlab/ | grep -E 'goal\s*=' | grep -v -E 'goal\s*=\s*"(key|orb|boss|normal|abort)"'
grep -rn 'CheckpointEvent(' tests/ python/spinlab/ | grep -E 'cp_type\s*=' | grep -v -E 'cp_type\s*=\s*"(midway|cp_entrance)"'
```

Expected set of non-canonical `goal=` values currently used in tests: `"goal"`, `"normal"` (canonical), `"exit"`, `"boss"` (canonical). The handler only branches on `event.goal == "abort"`, so any non-abort value is functionally a successful completion. Replace non-canonical values with `"normal"` (the closest match to "the level was completed normally") UNLESS the surrounding test context names a specific exit type (boss → keep "boss", goal → use "normal").

Record the exact list of files + line numbers in a scratchpad here:
```
# tests/unit/capture/test_recorder.py:36,56,77,94,116,151 — goal="goal" → "normal"
# tests/unit/capture/test_recorder.py:132 — goal="abort" (already canonical, leave)
# tests/unit/capture/test_recorder.py:192 — goal="exit" → "normal"
# tests/unit/capture/test_capture_with_conditions.py:31,51,57,72 — goal="goal" → "normal"
# tests/unit/test_session_manager.py:94,148,161,173,209 — goal="normal" or "abort" (already canonical)
# tests/unit/test_state_paths.py:65 — goal="goal" → "normal"
# tests/unit/test_timing.py:47,84,110,151,197,323 — mix of "normal" and "abort" (canonical)
```

Read each file before editing — the scratchpad above was derived from a prior grep but the line numbers may have drifted. Always re-grep before patching.

- [ ] **Step 2: Add Literal aliases in `protocol.py`**

In `python/spinlab/protocol.py`, near the top of the file (after the SPEED_UNCAPPED constant, before the Events section):

```python
from typing import Literal

# Values produced by retroarch/predicates.py::check_checkpoint_hit().
# Stored on CheckpointEvent.cp_type and persisted in the DB ``end_type``
# column for checkpoint segments (after recorder normalizes to "checkpoint").
CheckpointType = Literal["midway", "cp_entrance"]

# Values produced by retroarch/predicates.py::goal_type(). The recorder
# treats anything that isn't "abort" as a successful completion; ordering
# (key > orb > boss > normal > abort) is set by predicates.
LevelExitGoal = Literal["key", "orb", "boss", "normal", "abort"]
```

- [ ] **Step 3: Apply Literal types to the event fields**

Find `CheckpointEvent` (around line 36):

```python
@dataclass(frozen=True)
class CheckpointEvent:
    level_num: int = 0
    cp_ordinal: int = 1
    cp_type: str = ""
    state_path: str | None = None
    timestamp_ms: int = 0
    conditions: dict[str, int] = field(default_factory=dict)
```

Replace `cp_type: str = ""` with `cp_type: CheckpointType = "midway"` — the default changes from the placeholder `""` to a canonical value. (The detector always passes an explicit `cp_type`; the default exists for test construction convenience.)

Find `LevelExitEvent` (around line 61):

```python
@dataclass(frozen=True)
class LevelExitEvent:
    level: int = 0
    room: int = 0
    goal: str = "abort"
    elapsed_ms: int = 0
    frame: int = 0
    timestamp_ms: int = 0
    conditions: dict[str, int] = field(default_factory=dict)
```

Replace `goal: str = "abort"` with `goal: LevelExitGoal = "abort"` — default value stays the same.

- [ ] **Step 4: Update non-canonical test call sites**

Re-grep to get the current line numbers:
```bash
grep -rn 'LevelExitEvent(.*goal\s*=\s*"\(goal\|exit\)"' tests/
grep -rn 'CheckpointEvent(.*cp_type\s*=\s*"[^"]*"' tests/
```

For each match, edit the file to use a canonical value. Rule: `goal="goal"` → `goal="normal"`, `goal="exit"` → `goal="normal"`. Boss/orb/key/abort/normal are already canonical and stay.

Use the Edit tool per file; don't try `replace_all` in case any file has multiple distinct call sites that need different canonical mappings.

- [ ] **Step 5: Run pyright**

Run: `npx pyright python/`
Expected: 0 errors.

If pyright complains about test files (it shouldn't — pyright is configured for `python/` only, see `pyproject.toml:53`), the test edits in Step 4 were unnecessary for type-checking; they were necessary for runtime correctness only.

- [ ] **Step 6: Run full pytest**

Run: `python -m pytest -q --no-header`
Expected: 892 passed.

If any test fails with `goal="goal" is not "key" | "orb" | "boss" | "normal" | "abort"`, you missed a call site in Step 4. Find and fix.

- [ ] **Step 7: Frontend codegen sanity check**

This task doesn't touch api_schemas; codegen output should be identical. Confirm:
```bash
cd frontend && npm run gen-types && npm test && cd ..
```
Expected: green; `frontend/src/api-types.ts` diff should be empty (or trivially whitespace).

- [ ] **Step 8: Commit**

```bash
git add python/spinlab/protocol.py tests/
git commit -m "$(cat <<'EOF'
protocol: type LevelExitEvent.goal + CheckpointEvent.cp_type as Literals

predicates.goal_type() emits exactly key/orb/boss/normal/abort;
check_checkpoint_hit() emits exactly midway/cp_entrance. Encoding this as
Literal lets pyright catch typos at event-construction sites — recorder,
detector, and especially test fixtures (several of which were passing
goal="goal" or goal="exit", which happened to work because the only
branch is `goal == "abort"` but masked the real value set).

Test fixtures with non-canonical goal values normalized to "normal" —
the recorder treats anything-but-"abort" as completion, so the test
semantics are unchanged.

Protocol events are internal frozen dataclasses, not Pydantic models;
this is a pyright-only tightening, no OpenAPI/frontend-codegen impact.
EOF
)"
```

---

## Task 5: `ConditionSpec` dataclass for `SetConditionsCmd.definitions` + `replace_with_read_specs` (L5-Y2, L5-Y20)

**Goal:** Replace `SetConditionsCmd.definitions: list[dict]` and `ConditionRegistry.replace_with_read_specs(specs: list[dict])` with a typed `ConditionSpec` dataclass carrying `name: str`, `address: int`, `size: int`. Update the three call sites (`session_manager.install_condition_registry`, `orchestrator._on_set_conditions`, and the `replace_with_read_specs` body) and the three test files that construct dicts.

**Files:**
- Modify: `python/spinlab/protocol.py` — add `ConditionSpec` dataclass; retype `SetConditionsCmd.definitions`.
- Modify: `python/spinlab/condition_registry.py:115-132` — `replace_with_read_specs(specs: list[ConditionSpec])`; access fields instead of dict keys.
- Modify: `python/spinlab/session_manager.py:245-253` — construct `ConditionSpec` objects.
- Modify: `python/spinlab/retroarch/orchestrator.py:265-266` — no code change (passthrough), but verify the type flows.
- Modify: `tests/unit/retroarch/test_conditions.py:18-40` — construct `ConditionSpec` instead of dicts.
- Modify: `tests/unit/retroarch/test_orchestrator.py:247-248` — same.
- Modify: `tests/unit/retroarch/test_poller_conditions.py:45-47` — same.

### Steps

- [ ] **Step 1: Write the failing test**

Add a new test to `tests/unit/retroarch/test_conditions.py` BEFORE editing any production code:

```python
def test_replace_with_read_specs_accepts_condition_spec_objects():
    from spinlab.protocol import ConditionSpec
    reg = ConditionRegistry()
    reg.replace_with_read_specs([
        ConditionSpec(name="game_mode", address=0x100, size=1),
        ConditionSpec(name="counter", address=0x200, size=2),
    ])
    assert [d.name for d in reg.definitions] == ["game_mode", "counter"]
    assert reg.definitions[0].address == 0x100
    assert reg.definitions[1].size == 2
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest tests/unit/retroarch/test_conditions.py::test_replace_with_read_specs_accepts_condition_spec_objects -v`
Expected: FAIL — `ImportError: cannot import name 'ConditionSpec'` or similar.

- [ ] **Step 3: Add `ConditionSpec` and retype `SetConditionsCmd.definitions`**

In `python/spinlab/protocol.py`, in the Commands section (before `SetConditionsCmd`, around line 167):

```python
@dataclass(frozen=True)
class ConditionSpec:
    """A condition definition sent from SessionManager to the backend.

    The backend's poller uses this to issue NCI READ_CORE_RAM requests when
    stamping events with current memory-condition values. Only name + address
    + size are needed at read time; the type/values/scope used for capture-side
    decoding stay with the YAML-loaded ConditionDef in condition_registry.py.
    """
    name: str
    address: int
    size: int  # bytes; only 1 or 2 are supported by read_all
```

Then replace the `SetConditionsCmd`:

```python
@dataclass
class SetConditionsCmd:
    definitions: list[dict] = field(default_factory=list)
```

with:

```python
@dataclass
class SetConditionsCmd:
    definitions: list[ConditionSpec] = field(default_factory=list)
```

- [ ] **Step 4: Update `replace_with_read_specs` to take typed specs**

In `python/spinlab/condition_registry.py`, replace the existing `replace_with_read_specs`:

```python
def replace_with_read_specs(self, specs: list[dict]) -> None:
    """Replace ``definitions`` with read-only specs from ``SetConditionsCmd``.

    Each spec dict must have keys ``name`` (str), ``address`` (int),
    ``size`` (int in SUPPORTED_READ_SIZES). type/values/scope take their
    defaults — fine because only ``read_all`` touches definitions built
    this way; capture-side ``decode`` always uses YAML-loaded registries.
    """
    for s in specs:
        if s["size"] not in SUPPORTED_READ_SIZES:
            raise ValueError(
                f"unsupported condition size {s['size']} for {s['name']!r}; "
                f"only {SUPPORTED_READ_SIZES} supported"
            )
    self.definitions = [
        ConditionDef(name=s["name"], address=s["address"], size=s["size"])
        for s in specs
    ]
```

with:

```python
def replace_with_read_specs(self, specs: list["ConditionSpec"]) -> None:
    """Replace ``definitions`` with read-only specs from ``SetConditionsCmd``.

    Each ConditionSpec carries name/address/size; type/values/scope on the
    resulting ConditionDef take their defaults — fine because only
    ``read_all`` touches definitions built this way; capture-side ``decode``
    always uses YAML-loaded registries.
    """
    for s in specs:
        if s.size not in SUPPORTED_READ_SIZES:
            raise ValueError(
                f"unsupported condition size {s.size} for {s.name!r}; "
                f"only {SUPPORTED_READ_SIZES} supported"
            )
    self.definitions = [
        ConditionDef(name=s.name, address=s.address, size=s.size)
        for s in specs
    ]
```

And add the import at the top of `condition_registry.py`, in the `TYPE_CHECKING` block (introduce one if not present — see how other files use TYPE_CHECKING) to avoid a circular import with `protocol.py`:

```python
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from spinlab.protocol import ConditionSpec
```

The string-quoted `"ConditionSpec"` in the signature handles runtime resolution.

If TYPE_CHECKING already wouldn't help (e.g., the code path in `replace_with_read_specs` doesn't need the actual class at runtime — it only accesses `.name`, `.address`, `.size` attributes), then this is sufficient. Verify by running pyright after — if pyright is happy, the import is correct.

- [ ] **Step 5: Update `install_condition_registry` to build `ConditionSpec` objects**

In `python/spinlab/session_manager.py`, find the existing code around line 248-253:

```python
if self.emu.is_connected and registry.definitions:
    defs_payload = [
        {"name": d.name, "address": d.address, "size": d.size}
        for d in registry.definitions
    ]
    await self.emu.send_command(SetConditionsCmd(definitions=defs_payload))
```

Replace with:

```python
if self.emu.is_connected and registry.definitions:
    from spinlab.protocol import ConditionSpec
    defs_payload = [
        ConditionSpec(name=d.name, address=d.address, size=d.size)
        for d in registry.definitions
    ]
    await self.emu.send_command(SetConditionsCmd(definitions=defs_payload))
```

(Or hoist the import to the top of `session_manager.py` if `SetConditionsCmd` is already imported there; check first. If you hoist, drop the inline import.)

- [ ] **Step 6: Update the three test files**

`tests/unit/retroarch/test_conditions.py`:

```python
reg.replace_with_read_specs([
    {"name": "game_mode", "address": 0x100, "size": 1},
    {"name": "counter", "address": 0x200, "size": 2},
])
```

becomes:

```python
from spinlab.protocol import ConditionSpec
...
reg.replace_with_read_specs([
    ConditionSpec(name="game_mode", address=0x100, size=1),
    ConditionSpec(name="counter", address=0x200, size=2),
])
```

Apply analogously to lines 32-33 and 40 of the same file, and to `tests/unit/retroarch/test_orchestrator.py:247-248` and `tests/unit/retroarch/test_poller_conditions.py:45-47`.

The "test_unsupported_size_raises" test at `test_conditions.py:37-40` should also use `ConditionSpec(name="bad", address=0x100, size=4)` — the size=4 must still raise (it's the runtime validation in `replace_with_read_specs`, not a Pydantic-level check).

- [ ] **Step 7: Run the new test**

Run: `python -m pytest tests/unit/retroarch/test_conditions.py::test_replace_with_read_specs_accepts_condition_spec_objects -v`
Expected: PASS.

- [ ] **Step 8: Run full unit tests**

Run: `python -m pytest -m "not emulator" -q --no-header`
Expected: 881 passed (880 prior + 1 new).

- [ ] **Step 9: Run pyright**

Run: `npx pyright python/`
Expected: 0 errors. If TYPE_CHECKING import isn't enough, pyright will flag — add a regular import of `ConditionSpec` from `protocol.py` (no circular risk since `condition_registry.py` doesn't import from `protocol.py` otherwise; check).

- [ ] **Step 10: Run full pytest including emulator**

Run: `python -m pytest -q --no-header`
Expected: 893 passed (892 prior + 1 new).

- [ ] **Step 11: Frontend codegen sanity check**

`SetConditionsCmd` is not in the API surface (it's the dashboard → backend command stream, internal to Python). Codegen should be unaffected. Confirm:
```bash
cd frontend && npm run gen-types && cd ..
git diff frontend/src/api-types.ts
```
Expected: no diff.

- [ ] **Step 12: Commit**

```bash
git add python/spinlab/protocol.py python/spinlab/condition_registry.py python/spinlab/session_manager.py tests/unit/retroarch/test_conditions.py tests/unit/retroarch/test_orchestrator.py tests/unit/retroarch/test_poller_conditions.py
git commit -m "$(cat <<'EOF'
protocol+condition_registry: typed ConditionSpec for SetConditionsCmd

SetConditionsCmd.definitions and ConditionRegistry.replace_with_read_specs
took bare list[dict] with a docstring-enforced {name, address, size}
contract. Replacing both with list[ConditionSpec] (frozen dataclass) puts
the contract in the type system: pyright catches typos at construction
sites (session_manager, orchestrator), and the read-only specs flowing
from dashboard to RA backend are now uniformly typed.
EOF
)"
```

---

## Post-flight (after Task 5)

- [ ] **Verify full system state**

```bash
git log --oneline e9bbe01^..HEAD
```
Expected: 9 commits on the branch (4 trivials + 5 CF-4 tasks).

```bash
python -m pytest -q --no-header
npx pyright python/
ruff check python/
cd frontend && npm run gen-types && npm test && npm run build && cd ..
```

Expected: 893 backend tests pass, 0 pyright errors, ruff clean, frontend tests green, frontend build succeeds.

- [ ] **Spot-check codegen wins in `frontend/src/api-types.ts`**

Confirm the following TS unions now exist (not `string`):
- `Mode: "idle" | "reference" | "practice" | "replay" | "fill_gap" | "cold_fill" | "speed_run"`
- `EndpointType: "entrance" | "checkpoint" | "goal"`
- `ActionResponse.status: "ok" | "started" | "stopped" | "no_gaps"`
- `OkResponse.status: "ok" | "started" | "stopped" | "no_gaps"`
- `ModelOutput` and `Estimate` shapes unchanged (validated only — they should match the pre-CF-4 baseline since the conversion is a same-shape replacement).

- [ ] **Offer next steps to Andrew**

CF-4 is done. Remaining unpicked items from the 2026-05-15 scan that could come next:
- HL-A: Practice loop observability (medium)
- CF-2: Routes-through-SessionManager facade (medium-big)
- Cluster J: EstimatorParams / Priors dataclasses (trivial-medium)
- Cluster K: DB row → typed dataclasses (medium)
- L2-I4: Dual scheduler unification (medium)

---

## Self-Review Notes

**Spec coverage:**
- L5-Y1 (`ActionResponse.status: str`) → Task 2 ✓
- L5-Y2 (`SetConditionsCmd.definitions: list[dict]`) → Task 5 ✓
- L5-Y3 (`LevelExitEvent.goal: str`) → Task 4 ✓
- L5-Y4 (`cp_type`, `end_type`, `checkpoints` bare strings) → Task 4 covers `cp_type`. `end_type` in `PracticeLoadCmd` is left as `str` because its actual value set is broader than just `EndpointType` (the recorder normalizes to "checkpoint"/"goal" but PracticeLoadCmd carries the EndpointType-narrowed value from `SegmentCommand`). Documented as out-of-scope follow-up. `SpeedRunLoadCmd.checkpoints: list[dict]` is also left out — it requires its own `CheckpointDef` dataclass, separate ConditionSpec-style task; noted as follow-up.
- L5-Y5 (Status enum mismatch) → Task 2 ✓
- L5-Y20 (`replace_with_read_specs(list[dict])`) → Task 5 ✓
- L1-A10 (`ModelOutput` duplicate) → Task 3 ✓
- L5-Y17 (`EndpointType` duplicate) → Task 1 ✓
- L5-Y18 (`Mode` duplicate) → Task 1 ✓

**Follow-ups recorded above:**
- `PracticeLoadCmd.end_type: str` → defer (requires unraveling the broader end_type value set).
- `SpeedRunLoadCmd.checkpoints: list[dict]` → defer (separate small task with its own dataclass).

**Placeholder scan:** No "TBD" / "implement later" / "similar to Task N" patterns. Every step has the exact code to write or command to run.

**Type consistency:**
- `Mode`, `EndpointType`, `Status` are referenced consistently across Tasks 1-2: imported from `spinlab.models`.
- `Estimate` and `ModelOutput` referenced consistently across Task 3: imported from `spinlab.models` (now `pydantic.dataclasses.dataclass`).
- `LevelExitGoal` and `CheckpointType` defined in `protocol.py` (Task 4).
- `ConditionSpec` defined in `protocol.py` (Task 5); referenced from `condition_registry.py` via TYPE_CHECKING + string-quoted annotation, and from `session_manager.py` via regular import.
