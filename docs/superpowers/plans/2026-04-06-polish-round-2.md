# Polish Round 2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Five independent polish items — tighter log format, fix Vite proxy, live tuning sliders, log Clear All Data, and Python-side error logging.

**Architecture:** All items are independent. Tasks 1-2 are Python backend, Task 3 is config, Task 4 is frontend, Task 5 audits Python routes. Each task produces a self-contained commit.

**Tech Stack:** Python 3.11+ (logging, FastAPI), TypeScript (Vite, DOM), Vitest

---

### Task 1: Tighter log format

**Files:**
- Modify: `python/spinlab/cli.py:22-37`
- Modify: `tests/test_cli_logging.py`

- [ ] **Step 1: Write failing test for new log format**

Add to `tests/test_cli_logging.py`:

```python
def test_log_format_is_compact(tmp_path):
    """Log lines use MM-DD HH:MM:SS format without year, millis, or spinlab. prefix."""
    _setup_file_logging(tmp_path)
    log_path = tmp_path / "spinlab.log"
    logger = logging.getLogger("spinlab.test_compact")
    logger.info("hello compact")

    for handler in logging.root.handlers:
        handler.flush()

    content = log_path.read_text(encoding="utf-8")
    # Find the line with our test message
    line = [l for l in content.splitlines() if "hello compact" in l][-1]
    # No year prefix (e.g. "2026-")
    assert not line[:5].endswith("-"), f"Expected no year in: {line}"
    # No milliseconds (no comma followed by digits before INFO)
    assert ",0" not in line.split("INFO")[0] and ",1" not in line.split("INFO")[0]
    # Logger name should be "test_compact", not "spinlab.test_compact"
    assert "test_compact" in line
    assert "spinlab.test_compact" not in line
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_cli_logging.py::test_log_format_is_compact -v`
Expected: FAIL — current format includes year, millis, and `spinlab.` prefix.

- [ ] **Step 3: Update the formatter in cli.py**

In `python/spinlab/cli.py`, replace the `_setup_file_logging` function:

```python
def _setup_file_logging(data_dir: Path) -> None:
    """Configure rotating file log in data_dir/spinlab.log."""
    data_dir.mkdir(parents=True, exist_ok=True)
    log_path = data_dir / "spinlab.log"
    handler = RotatingFileHandler(
        str(log_path), maxBytes=1_000_000, backupCount=3, encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(shortname)s — %(message)s",
        datefmt="%m-%d %H:%M:%S",
    ))
    handler.addFilter(_StripPrefixFilter())
    handler.setLevel(logging.INFO)
    logging.root.addHandler(handler)
    logging.root.setLevel(min(logging.root.level or logging.INFO, logging.INFO))
    logging.getLogger("spinlab.cli").info(
        "==== Dashboard starting %s", "=" * 40
    )


class _StripPrefixFilter(logging.Filter):
    """Strip 'spinlab.' prefix from logger names for compact output."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.shortname = record.name.removeprefix("spinlab.")  # type: ignore[attr-defined]
        return True
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_cli_logging.py -v`
Expected: All tests pass (including existing ones).

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/cli.py tests/test_cli_logging.py
git commit -m "fix: tighten log format — drop year, millis, spinlab. prefix"
```

---

### Task 2: Log Clear All Data

**Files:**
- Modify: `python/spinlab/routes/system.py:1-2,54-64`
- Modify: `tests/test_cli_logging.py` (or a new route test — but no route test infra exists, so we verify via the log)

- [ ] **Step 1: Write failing test for reset logging**

Add to a new file `tests/test_reset_logging.py`:

```python
"""Test that POST /api/reset logs the action."""
import logging

import pytest
from unittest.mock import MagicMock, AsyncMock, patch


@pytest.mark.asyncio
async def test_reset_logs_warning_with_game_id():
    """POST /api/reset should log a warning with the game ID."""
    from spinlab.routes.system import reset_data

    mock_session = MagicMock()
    mock_session.stop_practice = AsyncMock()
    mock_session.mode = MagicMock()
    mock_session.mode.__eq__ = lambda self, other: False  # not REFERENCE
    mock_session.game_id = "abc123"
    mock_session.scheduler = MagicMock()

    mock_db = MagicMock()

    with patch("spinlab.routes.system.logger") as mock_logger:
        await reset_data(session=mock_session, db=mock_db)
        mock_logger.warning.assert_called_once()
        assert "abc123" in str(mock_logger.warning.call_args)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_reset_logging.py -v`
Expected: FAIL — `spinlab.routes.system` has no `logger` attribute yet.

- [ ] **Step 3: Add logger and warning to system.py**

In `python/spinlab/routes/system.py`, add the import and logger at the top (after the existing imports):

```python
import logging

logger = logging.getLogger(__name__)
```

Then in the `reset_data` function, add the log line before `db.reset_game_data(gid)`:

```python
@router.post("/reset")
async def reset_data(session: SessionManager = Depends(get_session), db: Database = Depends(get_db)):
    await session.stop_practice()
    if session.mode == Mode.REFERENCE:
        session._clear_ref_and_idle()
    gid = session.game_id
    if gid:
        logger.warning("reset: clearing all data for game=%s", gid)
        db.reset_game_data(gid)
    session.scheduler = None
    session.mode = Mode.IDLE
    return {"status": "ok"}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_reset_logging.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/routes/system.py tests/test_reset_logging.py
git commit -m "feat: log warning when Clear All Data is run"
```

---

### Task 3: Fix Vite proxy target

**Files:**
- Modify: `frontend/vite.config.ts:13-14`

- [ ] **Step 1: Fix the proxy target**

In `frontend/vite.config.ts`, change the proxy target from `http://localhost:8000` to `http://localhost:15483`:

```typescript
proxy: {
  "/api": {
    target: "http://localhost:15483",
    changeOrigin: true,
  },
},
```

- [ ] **Step 2: Verify the config is valid**

Run: `cd frontend && npx tsc --noEmit`
Expected: No errors (this is just a config object, but verify TypeScript is happy).

- [ ] **Step 3: Commit**

```bash
git add frontend/vite.config.ts
git commit -m "fix: point Vite dev proxy to correct dashboard port (15483)"
```

---

### Task 4: Debounced live tuning sliders

**Files:**
- Modify: `frontend/src/model.ts:287-379`
- Modify: `frontend/index.html:86-89`

- [ ] **Step 1: Add debounce helper and wire slider input events**

In `frontend/src/model.ts`, add a debounce timer at module level (near the top, after the existing `let` declarations):

```typescript
let _tuningDebounce: ReturnType<typeof setTimeout> | null = null;
const TUNING_DEBOUNCE_MS = 200;

function debouncedApply(): void {
  if (_tuningDebounce) clearTimeout(_tuningDebounce);
  _tuningDebounce = setTimeout(() => {
    applyTuningParams();
  }, TUNING_DEBOUNCE_MS);
}
```

- [ ] **Step 2: Wire debounced apply to slider and number input events**

In `renderTuningParams`, update the event listeners on the slider and number input (lines 315-320) to also trigger the debounced apply:

```typescript
    slider.addEventListener("input", () => {
      input.value = slider.value;
      debouncedApply();
    });
    input.addEventListener("input", () => {
      slider.value = input.value;
      debouncedApply();
    });
```

- [ ] **Step 3: Remove explicit apply from resetTuningDefaults**

The `resetTuningDefaults` function currently calls `applyTuningParams()` explicitly after setting slider values. Since setting `.value` programmatically does NOT fire `input` events, we still need one explicit call — but change it to a direct call (not debounced) so reset feels instant:

```typescript
async function resetTuningDefaults(): Promise<void> {
  if (!_tuningParams) return;
  _tuningParams.params.forEach((p) => {
    const slider = document.querySelector<HTMLInputElement>(
      '.tuning-slider[data-param="' + p.name + '"]',
    );
    const input = document.querySelector<HTMLInputElement>(
      '.tuning-value[data-param="' + p.name + '"]',
    );
    if (slider) slider.value = String(p.default);
    if (input) input.value = String(p.default);
  });
  await applyTuningParams();
}
```

This is unchanged from current code — keep the explicit `applyTuningParams()` since programmatic `.value` assignment doesn't fire `input` events.

- [ ] **Step 4: Remove Apply button from HTML**

In `frontend/index.html`, replace the tuning-actions div (lines 86-89):

```html
          <div class="tuning-actions">
            <button id="btn-tuning-reset" class="btn-sm">Reset Defaults</button>
          </div>
```

- [ ] **Step 5: Remove Apply button event listener from initModelTab**

In `frontend/src/model.ts`, in `initModelTab()`, remove line 375:

```typescript
  document.getElementById("btn-tuning-apply")?.addEventListener("click", applyTuningParams);
```

Keep the reset listener on the next line.

- [ ] **Step 6: Build and verify**

Run: `cd frontend && npm run build`
Expected: Build succeeds with no errors.

Run: `cd frontend && npm test`
Expected: All frontend tests pass.

- [ ] **Step 7: Commit**

```bash
git add frontend/src/model.ts frontend/index.html
git commit -m "feat: live debounced tuning sliders, remove Apply button"
```

---

### Task 5: Python-side error logging for API errors

**Files:**
- Modify: `python/spinlab/routes/model.py:1-2,98-110`
- Modify: `python/spinlab/routes/segments.py:1-2,40-49`

- [ ] **Step 1: Add logger to model.py and log validation errors**

In `python/spinlab/routes/model.py`, add after the existing imports:

```python
import logging

logger = logging.getLogger(__name__)
```

Then update `set_estimator_params` to log before raising:

```python
@router.post("/estimator-params")
def set_estimator_params(body: dict, session: SessionManager = Depends(get_session), db: Database = Depends(get_db)):
    sched = session._get_scheduler()
    est = sched.estimator
    params = body.get("params", {})
    # Validate param names
    valid_names = {p.name for p in est.declared_params()}
    for name in params:
        if name not in valid_names:
            logger.warning("set_estimator_params: unknown param %r (valid: %s)", name, valid_names)
            raise HTTPException(status_code=400, detail=f"Unknown param: {name}")
    db.save_allocator_config(f"estimator_params:{est.name}", json.dumps(params))
    sched.rebuild_all_states()
    return {"status": "ok"}
```

Also update `switch_estimator` to log:

```python
@router.post("/estimator")
def switch_estimator(body: dict, session: SessionManager = Depends(get_session)):
    from spinlab.estimators import list_estimators
    name = body.get("name")
    valid = list_estimators()
    if name not in valid:
        logger.warning("switch_estimator: unknown %r (valid: %s)", name, valid)
        raise HTTPException(status_code=400, detail=f"Unknown estimator: {name}. Valid: {valid}")
    sched = session._get_scheduler()
    sched.switch_estimator(name)
    return {"estimator": name}
```

And `set_allocator_weights`:

```python
@router.post("/allocator-weights")
def set_allocator_weights(body: dict, session: SessionManager = Depends(get_session)):
    sched = session._get_scheduler()
    try:
        sched.set_allocator_weights(body)
    except (ValueError, TypeError) as e:
        logger.warning("set_allocator_weights: %s", e)
        raise HTTPException(status_code=400, detail=str(e))
    return {"weights": body}
```

- [ ] **Step 2: Add logger to segments.py and log errors**

In `python/spinlab/routes/segments.py`, add after the existing imports:

```python
import logging

logger = logging.getLogger(__name__)
```

Then update `patch_segment` and `delete_segment`:

```python
@router.patch("/segments/{segment_id}")
def patch_segment(segment_id: str, body: SegmentPatch, db: Database = Depends(get_db)):
    if not db.segment_exists(segment_id):
        logger.warning("patch_segment: not found %s", segment_id)
        raise HTTPException(status_code=404, detail="segment not found")
    if body.is_primary is not None:
        db.set_segment_is_primary(segment_id, body.is_primary)
    other_fields = body.model_dump(exclude_none=True, exclude={"is_primary"})
    if other_fields:
        db.update_segment(segment_id, **other_fields)
    return {"ok": True, "id": segment_id, "is_primary": body.is_primary}


@router.delete("/segments/{segment_id}")
def delete_segment(segment_id: str, db: Database = Depends(get_db)):
    if not db.segment_exists(segment_id):
        logger.warning("delete_segment: not found %s", segment_id)
        raise HTTPException(status_code=404, detail="Segment not found")
    db.soft_delete_segment(segment_id)
    return {"status": "ok"}
```

- [ ] **Step 3: Run full test suite**

Run: `python -m pytest`
Expected: All tests pass. The logging additions are side-effect-free — they log but don't change behavior.

- [ ] **Step 4: Commit**

```bash
git add python/spinlab/routes/model.py python/spinlab/routes/segments.py
git commit -m "feat: add warning logs before HTTP error responses in routes"
```

---

### Final: Run full test suite

- [ ] **Step 1: Run all Python tests**

Run: `python -m pytest`
Expected: All pass.

- [ ] **Step 2: Run all frontend tests**

Run: `cd frontend && npm test`
Expected: All pass.
