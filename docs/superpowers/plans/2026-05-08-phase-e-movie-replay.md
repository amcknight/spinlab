# Phase E — Movie Replay Validation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

> **2026-05-08 amendment.** The Task 4 smoke test against live RA 1.22.2 found the originally-assumed NCI commands and file format wrong. Corrected facts now in the spec under "RA 1.22.2 movie format facts". Production code uses **`MovieRecorder`/`MoviePlayer`/`movie.py`** naming (avoids collision with `ReplayCmd`); NCI methods are `record_replay()`/`halt_replay()` for record (playback commands TBD by follow-up probe). On-disk format is `.replay<slot>` in `<savestate_directory>/<core_name>/`. The class names `BSVRecorder`/`BSVPlayer` and module `bsv.py` referenced below are obsolete — read every occurrence as `MovieRecorder`/`MoviePlayer`/`movie.py`. The fix-forward task between Task 4 and Task 5 lands these renames in production code.

**Goal:** Validate movie record/playback under our snes9x_libretro + runahead=2 setup by getting `tests/integration/test_replay_fixture.py` running through RetroArch, with movie recording integrated into reference runs as the fixture-producing path.

**Architecture:** New `python/spinlab/retroarch/movie.py` module owns `MovieRecorder` and `MoviePlayer` thin wrappers around NCI. The orchestrator's `_on_reference_start`/`_on_reference_stop` (currently no-ops) trigger recording; `_on_replay_cmd` (currently raises 501) drives playback. Three new integration tests gate progression: control-path smoke before the recorder, determinism + polling-during-playback after a real fixture exists.

**Tech Stack:** Python 3.11+ dataclasses, pytest + pytest-asyncio, RetroArch 1.22.2 with `snes9x_libretro.dll`, NCI over UDP 55355, libretro deterministic movie format.

**Spec:** [`docs/superpowers/specs/2026-05-08-phase-e-movie-replay-design.md`](../specs/2026-05-08-phase-e-movie-replay-design.md)

**Architectural deviation from spec.** The spec proposed adding `movie_recorder: MovieRecorder | None` to `ReferenceController.__init__`. This plan instead owns the recorder on `RetroArchOrchestrator` and triggers it from `_on_reference_start`/`_on_reference_stop`, keeping `ReferenceController` backend-agnostic. The reference run still produces `<refid>.replay` alongside `<refid>.mss`; only the wiring location differs.

**Definition of done:**
- All steps below complete with their tests passing
- `python -m pytest` runs clean
- Andrew has a fresh `tests/fixtures/love_yourself/one_level.replay` + sibling metadata file
- Mesen-side `test_replay_fixture.py` deleted; RA-side ports the assertions
- `RetroArchOrchestrator` no longer raises `BackendNotImplementedError` for `ReplayCmd`/`ReplayStopCmd`

---

## File structure

**New files:**
- `python/spinlab/retroarch/movie.py` — `MovieRecorder`, `MoviePlayer` classes and `discover_movie_dir` helper
- `tests/integration/test_movie_smoke.py` — three smoke tests (record toggle, determinism, polling-during-playback)
- `tests/unit/test_movie_recorder.py` — unit tests against fake NCI
- `tests/unit/test_movie_player.py` — unit tests against fake NCI
- `tests/fixtures/love_yourself/one_level.replay` — recorded by Andrew (Step 4); committed binary
- `tests/fixtures/love_yourself/one_level.json` — fixture metadata (frame count, expected segments, determinism probe)

**Modified files:**
- `python/spinlab/config.py` — add `EmulatorConfig.ra_movie_dir: Path | None`
- `python/spinlab/retroarch/nci.py` — add `record_replay()`, `play_replay()`, `halt_replay()`, `get_config_param(key)` methods
- `python/spinlab/retroarch/orchestrator.py` — wire `MovieRecorder` + `MoviePlayer`; replace `_unsupported_phase_e` for `ReplayCmd`/`ReplayStopCmd`; add `movie_recorder`/`movie_player` to `__init__` and `build_orchestrator`
- `tests/integration/conftest.py` — add `replay_ra_dashboard` fixture (RA equivalent of `replay_dashboard`)
- `tests/integration/test_replay_fixture.py` — port from `.spinrec`+Mesen to `.replay`+RA; delete Mesen-side variant

---

## Pre-flight

- [ ] **Read the spec.** Open [`docs/superpowers/specs/2026-05-08-phase-e-movie-replay-design.md`](../specs/2026-05-08-phase-e-movie-replay-design.md) and read the "Sequenced implementation" and "Anchoring, determinism, and the deep unknowns" sections. The plan below mirrors the steps but goes deeper.

- [ ] **Run the full test suite to establish a baseline.**

```bash
cd c:/Users/thedo/git/spinlab && python -m pytest
```

Expected: all tests pass or skip (the user's `feedback_fix_preexisting_failures.md` memory says any preexisting failures must be fixed before starting work — if anything fails, stop and fix it before continuing).

- [ ] **Confirm RA config is correct.** Open `C:\RetroArch-Win64\retroarch.cfg` and verify:
  - `cheevos_hardcore_mode_enable = "false"` (else movie record may silently no-op like savestates do)
  - `run_ahead_secondary_instance = "true"` (single-instance runahead corrupts state ops)
  - `network_cmd_enable = "true"` (NCI must be on)

If any are wrong, fix them and restart RetroArch before continuing.

---

## Task 1: Add NCI methods for movie control

**Files:**
- Modify: `python/spinlab/retroarch/nci.py`
- Test: `tests/unit/test_nci.py` (existing)

This adds the raw NCI primitives the rest of the plan builds on. Three commands plus a config-param read. We don't yet know which movie command works on RA 1.22.2 — the smoke test in Task 4 confirms — but the candidate set is well-known.

- [ ] **Step 1.1: Read the existing NCI client to match the style.**

Open `python/spinlab/retroarch/nci.py` and look at how `save_state()`, `pause_toggle()`, `frame_advance()` are implemented. They use `_send_no_reply()` — fire-and-forget. Movie commands follow the same shape.

- [ ] **Step 1.2: Write failing unit tests for the new NCI methods.**

Open `tests/unit/test_nci.py`. Find an existing test for a fire-and-forget command (e.g. `test_pause_toggle_sends_command`). Add four mirroring tests:

```python
def test_record_replay_sends_command(monkeypatch):
    sent = []
    client = NCIClient()
    monkeypatch.setattr(client, "_send_no_reply", lambda cmd: sent.append(cmd))
    client.record_replay()
    assert sent == ["RECORD_REPLAY"]


def test_play_replay_sends_command(monkeypatch):
    sent = []
    client = NCIClient()
    monkeypatch.setattr(client, "_send_no_reply", lambda cmd: sent.append(cmd))
    client.play_replay()
    assert sent == ["PLAY_REPLAY"]


def test_halt_replay_sends_command(monkeypatch):
    sent = []
    client = NCIClient()
    monkeypatch.setattr(client, "_send_no_reply", lambda cmd: sent.append(cmd))
    client.halt_replay()
    assert sent == ["HALT_REPLAY"]


def test_get_config_param_parses_reply(monkeypatch):
    client = NCIClient()
    monkeypatch.setattr(client, "_send", lambda cmd: 'GET_CONFIG_PARAM movie_directory "C:/RetroArch-Win64/states"')
    assert client.get_config_param("movie_directory") == "C:/RetroArch-Win64/states"
```

- [ ] **Step 1.3: Run tests, expect failure.**

```bash
python -m pytest tests/unit/test_nci.py -v -k "record_replay or play_replay or halt_replay or get_config_param"
```

Expected: 4 failures, `AttributeError: 'NCIClient' object has no attribute 'record_replay'` etc.

- [ ] **Step 1.4: Add the NCI methods.**

In `python/spinlab/retroarch/nci.py`, after the existing `frame_advance()` method, add:

```python
def record_replay(self) -> None:
    """Start movie recording.

    Fire-and-forget. RetroArch starts a new .replay file in its movie
    directory on record-on. The exact filename is chosen by RA; use the
    recorder's mtime-baseline pattern to discover it.

    Command `RECORD_REPLAY` confirmed working on RA 1.22.2 by Task 4 smoke test.
    """
    self._send_no_reply("RECORD_REPLAY")


def play_replay(self) -> None:
    """Start movie playback of whatever movie RA currently has loaded.

    Fire-and-forget. Loading the .replay file itself is out-of-band — typically
    via CLI flag at launch or via filesystem placement.

    NOTE: command name is provisional pending Task 4 follow-up probe.
    """
    self._send_no_reply("PLAY_REPLAY")


def halt_replay(self) -> None:
    """Stop movie recording or playback."""
    self._send_no_reply("HALT_REPLAY")


def get_config_param(self, key: str) -> str:
    """Read a runtime config param, e.g. 'movie_directory', 'savestate_directory'.

    Reply format: GET_CONFIG_PARAM <key> "<value>"
    """
    reply = self._send(f"GET_CONFIG_PARAM {key}")
    parts = reply.split(maxsplit=2)
    if len(parts) < 3:
        raise NCIProtocolError(f"GET_CONFIG_PARAM reply too short: {reply!r}")
    value = parts[2]
    if value.startswith('"') and value.endswith('"'):
        value = value[1:-1]
    return value
```

- [ ] **Step 1.5: Run tests, expect pass.**

```bash
python -m pytest tests/unit/test_nci.py -v -k "record_replay or play_replay or halt_replay or get_config_param"
```

Expected: 4 passes.

- [ ] **Step 1.6: Run the full test suite to confirm no regressions.**

```bash
python -m pytest
```

Expected: all green.

- [ ] **Step 1.7: Commit.**

```bash
git add python/spinlab/retroarch/nci.py tests/unit/test_nci.py
git commit -m "feat(retroarch): NCI methods for movie control + GET_CONFIG_PARAM

Adds record_replay, play_replay, halt_replay, get_config_param. RECORD_REPLAY
and HALT_REPLAY confirmed on RA 1.22.2 by Task 4 smoke test; PLAY_REPLAY
is provisional pending Task 4 follow-up probe.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 2: Add `ra_movie_dir` to config

**Files:**
- Modify: `python/spinlab/config.py`
- Test: `tests/unit/test_config.py` (existing)

The recorder needs to know where RA writes `.bsv` files so it can mtime-poll for the new file and rename it. Default points at the standard RA layout but is overridable.

- [ ] **Step 2.1: Write failing test for the new field.**

Open `tests/unit/test_config.py` and add a test alongside the existing `EmulatorConfig` tests:

```python
def test_ra_movie_dir_parsed_from_yaml(tmp_path):
    cfg_yaml = tmp_path / "config.yaml"
    cfg_yaml.write_text(
        "data:\n"
        "  dir: /tmp/data\n"
        "emulator:\n"
        "  backend: retroarch\n"
        "  ra_movie_dir: /custom/movies\n"
    )
    cfg = AppConfig.from_yaml(cfg_yaml)
    assert cfg.emulator.ra_movie_dir == Path("/custom/movies")


def test_ra_movie_dir_defaults_to_none():
    emu = EmulatorConfig()
    assert emu.ra_movie_dir is None
```

- [ ] **Step 2.2: Run test, expect failure.**

```bash
python -m pytest tests/unit/test_config.py -v -k "ra_movie_dir"
```

Expected: failures (`AttributeError: 'EmulatorConfig' object has no attribute 'ra_movie_dir'`).

- [ ] **Step 2.3: Add the field to `EmulatorConfig`.**

In `python/spinlab/config.py`, modify the `EmulatorConfig` dataclass:

```python
@dataclass
class EmulatorConfig:
    backend: str = "mesen-lua"
    path: Path | None = None
    lua_script: Path | None = None
    script_data_dir: Path | None = None
    retroarch_path: Path | None = None
    ra_core_path: Path | None = None
    savestate_dir: Path | None = None
    spinlab_state_dir: Path | None = None
    ra_game_basename: str | None = None
    ra_movie_dir: Path | None = None  # where RA writes .replay files; None → discover via NCI GET_CONFIG_PARAM
```

In `from_yaml`, parse the new field. Inside the `emu = raw.get("emulator", {})` block, add:

```python
ra_movie_dir = emu.get("ra_movie_dir")
```

And in the `EmulatorConfig(...)` constructor call, add:

```python
ra_movie_dir=Path(ra_movie_dir) if ra_movie_dir else None,
```

- [ ] **Step 2.4: Run test, expect pass.**

```bash
python -m pytest tests/unit/test_config.py -v -k "ra_movie_dir"
```

Expected: 2 passes.

- [ ] **Step 2.5: Commit.**

```bash
git add python/spinlab/config.py tests/unit/test_config.py
git commit -m "feat(config): add EmulatorConfig.ra_movie_dir for movie output dir

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 3: Build `MovieRecorder` (skeleton + unit tests)

**Files:**
- Create: `python/spinlab/retroarch/movie.py`
- Test: `tests/unit/test_movie_recorder.py`

The recorder owns the toggle-record / wait-for-file / move-to-final-path lifecycle. Stateful: tracks whether recording is active, where the destination is, and the mtime baseline for file discovery. NCI-only — no live RA needed for unit tests.

- [ ] **Step 3.1: Write failing unit tests against a fake NCI client.**

Create `tests/unit/test_movie_recorder.py`:

```python
"""Unit tests for MovieRecorder against a fake NCI client + tmp filesystem."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from spinlab.retroarch.movie import MovieRecorder
from spinlab.retroarch.exceptions import NCIProtocolError


@dataclass
class FakeNCI:
    """Records calls; doesn't touch the network."""
    calls: list[str] = field(default_factory=list)
    status_responsive: bool = True

    def record_replay(self) -> None:
        self.calls.append("record_replay")

    def halt_replay(self) -> None:
        self.calls.append("halt_replay")

    def get_status(self):
        if not self.status_responsive:
            raise NCIProtocolError("simulated unresponsive RA")
        return type("S", (), {"state": "PLAYING"})()


def test_recorder_starts_idle():
    rec = MovieRecorder(client=FakeNCI(), movie_dir=Path("/tmp"))
    assert not rec.is_recording()


def test_start_toggles_record_and_marks_active(tmp_path):
    fake = FakeNCI()
    rec = MovieRecorder(client=fake, movie_dir=tmp_path)
    rec.start(tmp_path / "out.replay")
    assert fake.calls == ["record_replay"]
    assert rec.is_recording()


def test_stop_toggles_record_polls_for_file_then_renames(tmp_path):
    fake = FakeNCI()
    rec = MovieRecorder(client=fake, movie_dir=tmp_path, _poll_interval_s=0.01)
    dest = tmp_path / "out.replay"
    rec.start(dest)

    # Simulate RA writing a .replay file on halt — the recorder should find it
    # via mtime baseline and move it to dest.
    ra_file = tmp_path / "RetroArch-auto.replay2"
    ra_file.write_bytes(b"RPLY" + b"\x00" * 100)

    result = rec.stop()
    assert result == dest
    assert dest.exists()
    assert not ra_file.exists()
    assert not rec.is_recording()


def test_stop_raises_if_no_new_replay_appears(tmp_path):
    fake = FakeNCI()
    rec = MovieRecorder(client=fake, movie_dir=tmp_path, _poll_interval_s=0.01, _poll_attempts=2)
    rec.start(tmp_path / "out.replay")
    with pytest.raises(FileNotFoundError):
        rec.stop()
    assert not rec.is_recording()


def test_stop_ignores_pre_existing_replay_files(tmp_path):
    fake = FakeNCI()
    # An old .replay file already in the dir — should NOT be picked up.
    old = tmp_path / "old.replay2"
    old.write_bytes(b"old content")
    rec = MovieRecorder(client=fake, movie_dir=tmp_path, _poll_interval_s=0.01, _poll_attempts=2)
    rec.start(tmp_path / "new.replay")
    with pytest.raises(FileNotFoundError):
        rec.stop()
    assert old.exists()  # old file untouched


def test_double_start_raises(tmp_path):
    rec = MovieRecorder(client=FakeNCI(), movie_dir=tmp_path)
    rec.start(tmp_path / "a.replay")
    with pytest.raises(RuntimeError, match="already recording"):
        rec.start(tmp_path / "b.replay")


def test_stop_without_start_raises(tmp_path):
    rec = MovieRecorder(client=FakeNCI(), movie_dir=tmp_path)
    with pytest.raises(RuntimeError, match="not recording"):
        rec.stop()
```

- [ ] **Step 3.2: Run tests, expect failure.**

```bash
python -m pytest tests/unit/test_movie_recorder.py -v
```

Expected: import error — `movie.py` doesn't exist yet.

- [ ] **Step 3.3: Create the `MovieRecorder` implementation.**

Create `python/spinlab/retroarch/movie.py`:

```python
"""libretro deterministic movie record/play wrappers.

Both classes drive RA via NCI. The recorder writes a movie file under RA's
movie directory and renames it to a SpinLab-keyed path on stop. The player
loads a SpinLab-keyed movie back into RA's movie directory before triggering
playback.

NCI commands and movie-file lifecycle are validated by the smoke tests in
tests/integration/test_movie_smoke.py before this module gets used in
production paths.
"""
from __future__ import annotations

import logging
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

logger = logging.getLogger(__name__)

# How long to wait after halt for RA to finalize the .replay file.
# 5 attempts × 200ms = 1s ceiling — RA finalizes essentially instantly when
# the command is processed, but the NCI command is fire-and-forget so we need
# a margin for command-processing latency.
_DEFAULT_POLL_INTERVAL_S = 0.2
_DEFAULT_POLL_ATTEMPTS = 5


class _NCIRecorder(Protocol):
    def record_replay(self) -> None: ...
    def halt_replay(self) -> None: ...
    def get_status(self): ...  # returns StatusInfo


@dataclass
class MovieRecorder:
    """Toggles movie recording and shuffles the resulting .replay file to a target path.

    Lifecycle:
      start(dest) — toggles record on, snapshots existing .replay files in
                    movie_dir as the baseline.
      stop()      — halts recording, polls movie_dir for a NEW .replay file
                    (anything not in the baseline), moves it to dest.
                    Returns the final path.
    """

    client: _NCIRecorder
    movie_dir: Path
    _active_dest: Path | None = field(default=None, init=False, repr=False)
    _baseline_files: set[Path] = field(default_factory=set, init=False, repr=False)
    _poll_interval_s: float = _DEFAULT_POLL_INTERVAL_S
    _poll_attempts: int = _DEFAULT_POLL_ATTEMPTS

    def is_recording(self) -> bool:
        return self._active_dest is not None

    def start(self, dest: Path) -> None:
        if self.is_recording():
            raise RuntimeError(f"already recording to {self._active_dest}")
        if not self.movie_dir.exists():
            self.movie_dir.mkdir(parents=True, exist_ok=True)
        self._baseline_files = set(self.movie_dir.glob("*.replay*"))
        self.client.record_replay()
        self._active_dest = dest
        logger.info("MovieRecorder.start: dest=%s baseline=%d files", dest, len(self._baseline_files))

    def stop(self) -> Path:
        if not self.is_recording():
            raise RuntimeError("not recording")
        dest = self._active_dest
        assert dest is not None
        self.client.halt_replay()
        # Poll for a new .replay file (not in baseline) appearing in movie_dir.
        new_file: Path | None = None
        for _ in range(self._poll_attempts):
            current = set(self.movie_dir.glob("*.replay*"))
            new_files = current - self._baseline_files
            if new_files:
                new_file = max(new_files, key=lambda p: p.stat().st_mtime)
                break
            time.sleep(self._poll_interval_s)
        self._active_dest = None
        self._baseline_files = set()
        if new_file is None:
            raise FileNotFoundError(
                f"MovieRecorder.stop: no new .replay file appeared in {self.movie_dir} "
                f"after {self._poll_attempts} attempts × {self._poll_interval_s}s"
            )
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(new_file), str(dest))
        logger.info("MovieRecorder.stop: %s → %s", new_file.name, dest)
        return dest


class _NCIPlayer(Protocol):
    def play_replay(self) -> None: ...
    def halt_replay(self) -> None: ...
    def get_status(self): ...


@dataclass
class MoviePlayer:
    """Stages a movie file into RA's movie_dir and triggers playback on/off.

    Stateless across plays — each call to play() copies the source into
    movie_dir under a deterministic name and tells RA to start playback.
    """

    client: _NCIPlayer
    movie_dir: Path
    _staged_path: Path | None = field(default=None, init=False, repr=False)
    _is_playing: bool = field(default=False, init=False, repr=False)
    _staged_name: str = "spinlab_replay.replay"

    def is_playing(self) -> bool:
        return self._is_playing

    def play(self, src: Path) -> None:
        if self._is_playing:
            raise RuntimeError("already playing")
        if not src.exists():
            raise FileNotFoundError(f"movie source not found: {src}")
        self.movie_dir.mkdir(parents=True, exist_ok=True)
        staged = self.movie_dir / self._staged_name
        shutil.copy2(str(src), str(staged))
        self._staged_path = staged
        self.client.play_replay()
        self._is_playing = True
        logger.info("MoviePlayer.play: %s → %s", src, staged)

    def stop(self) -> None:
        if not self._is_playing:
            return  # idempotent stop, like NCI's pause_toggle gating
        self.client.halt_replay()
        self._is_playing = False
        if self._staged_path is not None and self._staged_path.exists():
            try:
                self._staged_path.unlink()
            except OSError as exc:
                logger.warning("MoviePlayer.stop: could not unlink staged %s: %s",
                               self._staged_path, exc)
        self._staged_path = None
        logger.info("MoviePlayer.stop")


def discover_movie_dir(client) -> Path:
    """Read RA's movie_directory via NCI GET_CONFIG_PARAM.

    Used at orchestrator construction time when EmulatorConfig.ra_movie_dir
    is None (auto-discovery). If RA reports a relative path, returns it as-is
    — caller is responsible for resolution if needed.
    """
    raw = client.get_config_param("movie_directory")
    return Path(raw)
```

- [ ] **Step 3.4: Run tests, expect pass.**

```bash
python -m pytest tests/unit/test_movie_recorder.py -v
```

Expected: all 7 tests pass.

- [ ] **Step 3.5: Run full suite to confirm no regressions.**

```bash
python -m pytest
```

Expected: all green.

- [ ] **Step 3.6: Commit.**

```bash
git add python/spinlab/retroarch/movie.py tests/unit/test_movie_recorder.py
git commit -m "feat(retroarch): MovieRecorder skeleton with mtime-baseline file discovery

Stateful recorder that snapshots movie_dir on start, polls for the new
.replay file after halt_replay, then moves it to a SpinLab-keyed dest path.
Unit tests against a fake NCI client cover lifecycle, error paths, and
ignoring pre-existing files in movie_dir.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 4: Smoke test — movie record toggle works on real RA

**Files:**
- Create: `tests/integration/test_movie_smoke.py`

This is the gate. If the canonical NCI command name is wrong on RA 1.22.2 the test fails loudly and we triage before going further. The test uses the existing `ra_harness` session fixture, mirrors the existing `test_transitions.py` style.

- [ ] **Step 4.1: Write the smoke test.**

Create `tests/integration/test_movie_smoke.py`:

```python
"""Smoke tests for movie record/playback against a live headless RetroArch.

These are sequenced gates — the record-toggle test must pass before the
recorder integration in Task 5 is meaningful. The determinism and
polling-during-playback tests in Tasks 7-8 require Andrew to first record
a real fixture under Task 6.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from spinlab.retroarch.movie import MovieRecorder, discover_movie_dir
from tests.integration.ra_harness import RAHarness

pytestmark = pytest.mark.emulator


def _frame_advance_n(harness: RAHarness, n: int) -> None:
    for _ in range(n):
        harness.client.frame_advance()


def test_record_toggle_creates_file(ra_harness: RAHarness, tmp_path: Path):
    """Toggle movie record on, advance frames, halt, expect a new .replay file."""
    movie_dir = discover_movie_dir(ra_harness.client)
    assert movie_dir.exists(), f"RA reports movie_directory={movie_dir!r} but it doesn't exist"

    recorder = MovieRecorder(client=ra_harness.client, movie_dir=movie_dir)
    dest = tmp_path / "smoke.replay"

    recorder.start(dest)
    _frame_advance_n(ra_harness, 30)
    recorder.stop()

    assert dest.exists(), f"MovieRecorder.stop did not produce {dest}"
    assert dest.stat().st_size > 0, f"{dest} is empty"

    # Confirm RA is still responsive — no deep-pause, no crash.
    status = ra_harness.client.get_status()
    assert status.state in ("PAUSED", "PLAYING"), (
        f"RA in unexpected state {status.state!r} after movie record"
    )

    # Confirm FRAMEADVANCE still ticks the core.
    snap_before = ra_harness.client.read_ram(0x0000, 16)
    ra_harness.client.frame_advance()
    time.sleep(0.05)
    snap_after = ra_harness.client.read_ram(0x0000, 16)
    assert snap_before != snap_after, (
        "FRAMEADVANCE no longer ticks core after movie record toggle"
    )
```

- [ ] **Step 4.2: Run the smoke test against live RA.**

```bash
python -m pytest tests/integration/test_movie_smoke.py -v -m emulator
```

**Expected outcome 1 (the happy path):** test passes. The NCI command `RECORD_REPLAY` works on RA 1.22.2 and the recorder finds the file. Proceed to Task 5.

**Expected outcome 2 (command name wrong):** test fails because `recorder.stop()` raises `FileNotFoundError` — RA never wrote a .replay file. STOP. Investigate alternatives in this order:
1. Check `C:/RetroArch-Win64/.config/retroarch/retroarch.cfg` for `input_movie_record_toggle = "..."` — the bound key name hints at the NCI command name (RA's NCI command names mirror its input action names).
2. Try `MOVIE_RECORD_TOGGLE` instead. Update `nci.py` Task 1.4 with the corrected command name.
3. If neither works, try a `--record <path>` CLI flag on RA launch — that's a fallback path that requires changing `RAHarness` to accept a movie path.
4. If nothing works, halt the plan and update the spec's "Risks and mitigations" section.

**Expected outcome 3 (RA crashes / deep-pause):** test fails on the responsiveness check. STOP. This indicates movie record toggle has destabilizing side effects on our setup. Investigate before continuing.

- [ ] **Step 4.3: If the test passed unmodified, commit.**

```bash
git add tests/integration/test_movie_smoke.py
git commit -m "test(retroarch): movie record-toggle smoke test against live RA

Validates RECORD_REPLAY NCI command produces a .replay file and leaves
RA responsive. Gate for the recorder integration in subsequent tasks.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

If the test required NCI command-name fixes in Task 1, also include those in the same commit (`git add python/spinlab/retroarch/nci.py`).

---

## Task 5: Wire `MovieRecorder` into the orchestrator

**Files:**
- Modify: `python/spinlab/retroarch/orchestrator.py`
- Test: `tests/unit/test_retroarch_orchestrator.py` (existing)

The orchestrator's `_on_reference_start` and `_on_reference_stop` are currently no-ops (logging only). This task makes them trigger movie recording. The `MovieRecorder` instance is constructed in `build_orchestrator` and passed in.

- [ ] **Step 5.1: Read the existing orchestrator to find the no-op handlers.**

Open `python/spinlab/retroarch/orchestrator.py`. Find `_on_reference_start` (around line 317) and `_on_reference_stop` (around line 323). Find `build_orchestrator` (around line 397).

- [ ] **Step 5.2: Write failing unit tests for the orchestrator wiring.**

Open `tests/unit/test_retroarch_orchestrator.py` (or create it if it doesn't exist — check first with `ls tests/unit/test_retroarch_orchestrator.py 2>/dev/null`). Add tests that exercise the `_on_reference_start` / `_on_reference_stop` paths with a fake recorder:

```python
import pytest
from pathlib import Path

from spinlab.protocol import ReferenceStartCmd, ReferenceStopCmd


class FakeMovieRecorder:
    def __init__(self):
        self.started_with: Path | None = None
        self.stopped: bool = False

    def start(self, dest: Path) -> None:
        self.started_with = dest

    def stop(self) -> Path:
        self.stopped = True
        return self.started_with  # type: ignore[return-value]

    def is_recording(self) -> bool:
        return self.started_with is not None and not self.stopped


@pytest.mark.asyncio
async def test_on_reference_start_triggers_recorder(orchestrator_with_fake_recorder):
    orch, fake_rec = orchestrator_with_fake_recorder
    spinrec = "/data/game/rec/refid.spinrec"
    await orch._on_reference_start(ReferenceStartCmd(path=spinrec))
    assert fake_rec.started_with == Path("/data/game/rec/refid.replay")


@pytest.mark.asyncio
async def test_on_reference_stop_triggers_recorder_stop(orchestrator_with_fake_recorder):
    orch, fake_rec = orchestrator_with_fake_recorder
    await orch._on_reference_start(ReferenceStartCmd(path="/x/y/z.spinrec"))
    await orch._on_reference_stop(ReferenceStopCmd())
    assert fake_rec.stopped


@pytest.mark.asyncio
async def test_on_reference_start_logs_warning_if_recorder_fails(
    orchestrator_with_failing_recorder, caplog,
):
    orch, _ = orchestrator_with_failing_recorder
    # Should NOT raise — failures are non-fatal.
    await orch._on_reference_start(ReferenceStartCmd(path="/x/y/z.spinrec"))
    assert any("movie recording failed" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_on_reference_start_skips_when_recorder_is_none(
    orchestrator_without_recorder,
):
    # No-op path preserved for installs that don't have ra_movie_dir configured.
    orch = orchestrator_without_recorder
    await orch._on_reference_start(ReferenceStartCmd(path="/x/y/z.spinrec"))
    # No exception, no behavior change from current state.
```

You'll need fixtures for `orchestrator_with_fake_recorder`, `orchestrator_with_failing_recorder`, `orchestrator_without_recorder`. Add at the top of the file (or use the existing test fixtures pattern — check the file's existing structure):

```python
import pytest

from spinlab.retroarch.orchestrator import RetroArchOrchestrator


class FakeNCI:
    def get_status(self):
        return type("S", (), {"state": "PLAYING", "system": None, "game": None, "crc32": None})()


class FakeStateIO:
    def update_game_basename(self, name): pass
    def resolve_event_path(self, ev): return None


class FakePoller:
    deps = type("D", (), {"on_event": lambda *a: None})()
    period_sec = 0.016
    async def run(self): pass
    async def stop(self): pass



def _build_orch(recorder):
    return RetroArchOrchestrator(
        client=FakeNCI(),
        state_io=FakeStateIO(),
        poller=FakePoller(),
        conditions=None,
        practice_timing=None,
        speed_run_timing=None,
        movie_recorder=recorder,
    )


@pytest.fixture
def orchestrator_with_fake_recorder():
    rec = FakeMovieRecorder()
    return _build_orch(rec), rec


@pytest.fixture
def orchestrator_with_failing_recorder():
    class FailingRec:
        def start(self, dest):
            raise RuntimeError("simulated failure")
        def is_recording(self): return False
    rec = FailingRec()
    return _build_orch(rec), rec


@pytest.fixture
def orchestrator_without_recorder():
    return _build_orch(None)
```

If `tests/unit/test_retroarch_orchestrator.py` doesn't exist, create it with the imports + fixtures + tests above as one file.

- [ ] **Step 5.3: Run tests, expect failures.**

```bash
python -m pytest tests/unit/test_retroarch_orchestrator.py -v -k "reference_start or reference_stop"
```

Expected: failures (`TypeError: __init__ got unexpected keyword 'movie_recorder'`, etc.).

- [ ] **Step 5.4: Add `movie_recorder` to `RetroArchOrchestrator.__init__`.**

In `python/spinlab/retroarch/orchestrator.py`, find the `__init__` signature (around line 65). Add `movie_recorder` as a kwarg:

```python
def __init__(
    self,
    client,
    state_io,
    poller,
    conditions,
    practice_timing,
    speed_run_timing,
    movie_recorder=None,  # Optional[MovieRecorder]
):
    # ... existing body ...
    self._movie_recorder = movie_recorder
```

Also add the import near the top of the file:

```python
from spinlab.retroarch.movie import MovieRecorder
```

(Add as `if TYPE_CHECKING:` if there's a circular import concern — check the file's existing import patterns.)

- [ ] **Step 5.5: Replace `_on_reference_start` with the recorder-triggering version.**

In `python/spinlab/retroarch/orchestrator.py`, replace the existing `_on_reference_start` body:

```python
async def _on_reference_start(self, cmd: ReferenceStartCmd) -> None:
    """Trigger movie recording if a recorder is configured. Failures are
    non-fatal — reference runs are about state captures; movie recording is supplementary.
    """
    if self._movie_recorder is None:
        logger.info("Reference recording started (no movie recorder configured)")
        return
    replay_path = Path(cmd.path).with_suffix(".replay")
    try:
        await asyncio.to_thread(self._movie_recorder.start, replay_path)
        logger.info("movie recording started: %s", replay_path)
    except Exception as exc:
        logger.warning("movie recording failed to start: %s", exc)
```

- [ ] **Step 5.6: Replace `_on_reference_stop` with the recorder-stopping version.**

Replace the `_on_reference_stop` body:

```python
async def _on_reference_stop(self, cmd: ReferenceStopCmd) -> None:
    """Stop movie recording if active. Failures are non-fatal."""
    if self._movie_recorder is None or not self._movie_recorder.is_recording():
        logger.info("Reference recording stopped (no movie recorder active)")
        return
    try:
        path = await asyncio.to_thread(self._movie_recorder.stop)
        logger.info("movie recording stopped: %s", path)
    except Exception as exc:
        logger.warning("movie recording failed to stop: %s", exc)
```

Make sure `from pathlib import Path` and `import asyncio` are imported at the top of the file (they almost certainly already are; verify).

- [ ] **Step 5.7: Update `build_orchestrator` to construct and pass the recorder.**

In `python/spinlab/retroarch/orchestrator.py`, find `build_orchestrator` (around line 397). After the `state_io` construction and before the `RetroArchOrchestrator(...)` call, add:

```python
from spinlab.retroarch.movie import MovieRecorder, discover_movie_dir

if emu.ra_movie_dir is not None:
    movie_dir = emu.ra_movie_dir
else:
    try:
        movie_dir = discover_movie_dir(client)
    except Exception as exc:
        logger.warning(
            "build_orchestrator: movie recorder disabled — could not discover movie_dir: %s",
            exc,
        )
        movie_dir = None

movie_recorder = MovieRecorder(client=client, movie_dir=movie_dir) if movie_dir is not None else None
```

And pass `movie_recorder=movie_recorder` into the `RetroArchOrchestrator(...)` constructor call.

- [ ] **Step 5.8: Run unit tests, expect pass.**

```bash
python -m pytest tests/unit/test_retroarch_orchestrator.py -v -k "reference_start or reference_stop"
```

Expected: 4 passes.

- [ ] **Step 5.9: Run full suite, expect all green.**

```bash
python -m pytest
```

Expected: all green. If anything fails, fix before committing.

- [ ] **Step 5.10: Commit.**

```bash
git add python/spinlab/retroarch/orchestrator.py tests/unit/test_retroarch_orchestrator.py
git commit -m "feat(retroarch): wire MovieRecorder into orchestrator reference handlers

_on_reference_start/_stop now trigger movie recording if a recorder
is configured. Recorder is constructed in build_orchestrator from
EmulatorConfig.ra_movie_dir (or auto-discovered via NCI). Failures are
non-fatal — state captures remain the primary reference-run output.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 6: Andrew records the fixture (manual)

**Files:**
- Create: `tests/fixtures/love_yourself/one_level.replay` (binary)
- Create: `tests/fixtures/love_yourself/one_level.json` (metadata)

This is **a manual step the human user performs.** Cannot be automated by an agent. The plan halts here until Andrew confirms the fixture is recorded.

- [ ] **Step 6.1: Andrew launches the dashboard against RA with Love Yourself.**

```bash
spinlab dashboard --config config.yaml
```

(Verify `emulator.backend == "retroarch"` and `rom.dir` points at the dir containing `Love Yourself.smc`.)

- [ ] **Step 6.2: Andrew starts a reference run, plays through one level, finishes the level (reach goal), and saves the run with name "phase_e_fixture_one_level".**

The dashboard's reference recording flow handles state captures + (now) movie recording. After save, two files exist in the data dir:

- `{data_dir}/{game_id}/rec/{ref_id}.mss`
- `{data_dir}/{game_id}/rec/{ref_id}.replay`  ← the new one

If the `.replay` file is missing, movie recording silently failed during the run. Check `{data_dir}/spinlab.log` for "movie recording failed" warnings. If present, debug Task 4 / Task 5 wiring before continuing.

- [ ] **Step 6.3: Andrew copies the fixture to the test fixtures dir.**

```bash
# From project root, with the actual ref_id substituted:
cp "{data_dir}/{game_id}/rec/{ref_id}.replay" tests/fixtures/love_yourself/one_level.replay
```

- [ ] **Step 6.4: Andrew creates the metadata file.**

Create `tests/fixtures/love_yourself/one_level.json` with the fixture's actual numbers. Frame count comes from the dashboard's `/api/state` `replay.total` after a quick replay-back (you can do this once the player wiring lands in Task 9 — for now, leave `frame_count: -1` and update later, or eyeball the ref-run's logged duration × 60). Determinism probe should be a memory address that has a stable value mid-level (Mario's X position, or any in-level RAM that doesn't depend on RNG):

```json
{
  "frame_count": 3500,
  "expected_segments": 2,
  "determinism_probe": {
    "frame": 1000,
    "addr": 148,
    "expected_byte": 32,
    "comment": "Mario X-low byte at frame 1000 — replace with actual measured value"
  }
}
```

The actual numbers come out of Task 7 (determinism test). For now write the file with placeholder numbers; Task 7 will refine.

- [ ] **Step 6.5: Andrew commits the fixture.**

```bash
git add tests/fixtures/love_yourself/one_level.replay tests/fixtures/love_yourself/one_level.json
git commit -m "fixtures: love_yourself one_level.replay recorded via reference-run movie path

Phase E test fixture, replacing the Mesen-era two_level.spinrec. Recorded
on $(date) via the dashboard reference-run flow with backend=retroarch.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 7: Smoke test — movie playback determinism

**Files:**
- Modify: `tests/integration/test_movie_smoke.py`
- Modify: `tests/fixtures/love_yourself/one_level.json` (refine numbers)

This validates two things in one test: (a) movie playback works at all, and (b) the same playback produces identical memory at the same frame, twice in a row.

- [ ] **Step 7.1: Add the determinism test to `test_movie_smoke.py`.**

Append to `tests/integration/test_movie_smoke.py`:

```python
import json

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "love_yourself"


def _load_fixture_metadata() -> dict:
    return json.loads((FIXTURE_DIR / "one_level.json").read_text())


def _play_and_read_at_frame(harness: RAHarness, fixture: Path, frame: int, addr: int) -> int:
    """Start playback, advance to `frame`, read byte at `addr`, stop."""
    from spinlab.retroarch.movie import MoviePlayer, discover_movie_dir
    movie_dir = discover_movie_dir(harness.client)
    player = MoviePlayer(client=harness.client, movie_dir=movie_dir)
    player.play(fixture)
    try:
        for _ in range(frame):
            harness.client.frame_advance()
        # Small delay to let playback settle on the frame.
        time.sleep(0.05)
        b = harness.client.read_ram(addr, 1)[0]
    finally:
        player.stop()
    return b


@pytest.mark.skipif(
    not (FIXTURE_DIR / "one_level.replay").exists(),
    reason="one_level.replay fixture not recorded yet (Task 6)",
)
def test_movie_playback_deterministic(ra_harness: RAHarness):
    """Same fixture, played twice in the same RA session, must produce identical
    memory at the same frame. Validates movie playback determinism under our
    RA config (runahead=2, secondary-instance=true, cheevos-off).
    """
    meta = _load_fixture_metadata()
    probe = meta["determinism_probe"]
    fixture = FIXTURE_DIR / "one_level.replay"

    byte_run_1 = _play_and_read_at_frame(ra_harness, fixture, probe["frame"], probe["addr"])
    byte_run_2 = _play_and_read_at_frame(ra_harness, fixture, probe["frame"], probe["addr"])

    # If you're seeing this fail with byte_run_1 != probe["expected_byte"],
    # update one_level.json with the actual measured value (probe is bootstrapped
    # via the first successful run — see Task 7.3).
    assert byte_run_1 == byte_run_2, (
        f"Non-deterministic playback: run1={byte_run_1:#x} run2={byte_run_2:#x} "
        f"at frame={probe['frame']} addr={probe['addr']:#x}"
    )
    assert byte_run_1 == probe["expected_byte"], (
        f"Memory at frame {probe['frame']} addr {probe['addr']:#x} = {byte_run_1:#x}, "
        f"expected {probe['expected_byte']:#x} (per fixture metadata)"
    )
```

- [ ] **Step 7.2: Run the test (with placeholder metadata).**

```bash
python -m pytest tests/integration/test_movie_smoke.py::test_movie_playback_deterministic -v
```

**Expected outcome 1:** the determinism check passes (`byte_run_1 == byte_run_2`) but the expected_byte assertion fails because metadata is placeholder. The error message tells you the actual measured byte.

**Expected outcome 2:** determinism check fails (`byte_run_1 != byte_run_2`). STOP. This means movie playback under runahead=2 is non-deterministic on our setup. Investigate:
1. Is `cheevos_hardcore_mode_enable = "false"` in `retroarch.cfg`? Re-verify.
2. Is `run_ahead_secondary_instance = "true"`? Re-verify.
3. Try with runahead disabled entirely (set `run_ahead_enabled = "false"` temporarily) — if determinism returns, the issue is runahead-specific and Phase E may need to gate replay on disabling runahead.

**Expected outcome 3:** test errors out with NCI errors or RA hangs. STOP. Movie playback may be incompatible with our config; back off and investigate before going further.

- [ ] **Step 7.3: Update `one_level.json` with the actual measured value.**

Edit `tests/fixtures/love_yourself/one_level.json`, replace `expected_byte` with the actual byte the previous run measured. Adjust `frame` and `addr` if the chosen probe address turns out to be a frame counter or RNG byte (which would still be deterministic under movie playback but is a confusing probe).

- [ ] **Step 7.4: Re-run the test, expect pass.**

```bash
python -m pytest tests/integration/test_movie_smoke.py::test_movie_playback_deterministic -v
```

Expected: pass.

- [ ] **Step 7.5: Commit fixture metadata + the test.**

```bash
git add tests/integration/test_movie_smoke.py tests/fixtures/love_yourself/one_level.json
git commit -m "test(retroarch): movie playback determinism smoke test

Validates two consecutive playbacks of one_level.replay produce identical
memory at the determinism-probe frame. Refines fixture metadata with
the measured value.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 8: Smoke test — Poller runs during movie playback

**Files:**
- Modify: `tests/integration/test_movie_smoke.py`

Validates that the production memory poller (which feeds `TransitionDetector`) can keep up while RA is in movie playback. The replay-fixture test in Task 10 depends on this — if the poller starves, transitions get missed during replay.

- [ ] **Step 8.1: Read the existing Poller to understand its run loop.**

Open `python/spinlab/retroarch/poller.py`. Note `Poller.run()` (the async loop) and `PollerDeps`. The test will construct a poller with a counting on_event callback and run it for K frames of movie playback.

- [ ] **Step 8.2: Add the polling test to `test_movie_smoke.py`.**

Append to `tests/integration/test_movie_smoke.py`:

```python
import asyncio

from spinlab.retroarch.poller import Poller, PollerDeps, DEFAULT_PERIOD_SEC
from spinlab.retroarch.snapshot import read_snapshot


@pytest.mark.skipif(
    not (FIXTURE_DIR / "one_level.replay").exists(),
    reason="one_level.replay fixture not recorded yet (Task 6)",
)
def test_poller_runs_during_playback(ra_harness: RAHarness):
    """Poller reads RAM at 60Hz during movie playback without errors or starvation."""
    from spinlab.retroarch.movie import MoviePlayer, discover_movie_dir
    fixture = FIXTURE_DIR / "one_level.replay"
    movie_dir = discover_movie_dir(ra_harness.client)
    player = MoviePlayer(client=ra_harness.client, movie_dir=movie_dir)

    target_seconds = 1.0
    target_frames = int(target_seconds / DEFAULT_PERIOD_SEC)  # ~60 frames at 60Hz

    events_seen: list = []
    deps = PollerDeps(
        client=ra_harness.client,
        read_snapshot=read_snapshot,
        on_event=lambda ev: events_seen.append(ev),
        state_path_for=lambda ev: None,
        conditions_registry=None,
    )
    poller = Poller(deps, period_sec=DEFAULT_PERIOD_SEC)

    async def _run_for(seconds: float):
        task = asyncio.create_task(poller.run())
        await asyncio.sleep(seconds)
        await poller.stop()
        try:
            await asyncio.wait_for(task, timeout=2.0)
        except asyncio.TimeoutError:
            task.cancel()

    player.play(fixture)
    try:
        # During movie playback the core advances frames automatically — no
        # frame_advance() needed. Just let the poller hammer NCI for K frames.
        asyncio.run(_run_for(target_seconds))
    finally:
        player.stop()

    # Read poller's internal counters if available; otherwise count via on_event.
    # The pure success metric is "no crash, poller hit roughly the expected rate."
    # 90% threshold gives margin for OS scheduling jitter.
    expected = int(target_frames * 0.9)
    actual = poller.poll_count if hasattr(poller, "poll_count") else len(events_seen)
    assert actual >= expected, (
        f"Poller hit {actual} polls in {target_seconds}s, expected ≥{expected}. "
        f"movie playback may be starving the poller."
    )
```

If `Poller` doesn't expose a `poll_count` attribute (check `python/spinlab/retroarch/poller.py`), use `len(events_seen)` only — but be aware that no-event polls won't be counted, so this becomes a less-strict check. If needed, add a `poll_count` attribute to `Poller` as part of this task (small change, increment in the run loop).

- [ ] **Step 8.3: Run the test against live RA.**

```bash
python -m pytest tests/integration/test_movie_smoke.py::test_poller_runs_during_playback -v
```

**Expected outcome 1:** test passes. Poller keeps up. Proceed to Task 9.

**Expected outcome 2:** test fails with poll count below threshold. Mitigation per spec: throttle movie playback speed via NCI's `SLOWMOTION_RATIO` or equivalent. This requires:
1. Add `client.set_slowmotion_ratio(ratio)` to `nci.py` if not present.
2. Have `MoviePlayer.play(src, speed_ratio=1.0)` accept a slow-motion factor.
3. Re-run the test with `speed_ratio=2.0` (half speed).
4. Document in the spec that replay runs at half speed under RA.

**Expected outcome 3:** test errors with NCI exceptions. STOP. The poller is conflicting with playback at the protocol level — investigate whether `READ_CORE_RAM` blocks playback or vice versa.

- [ ] **Step 8.4: Commit.**

```bash
git add tests/integration/test_movie_smoke.py python/spinlab/retroarch/poller.py
git commit -m "test(retroarch): poller-during-movie-playback smoke test

Validates the production memory poller can keep up with movie playback at
60Hz under our NCI transport. Threshold 90% of expected polls allows for
OS scheduling jitter without masking real starvation.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 9: Wire `MoviePlayer` into `_on_replay_cmd`

**Files:**
- Modify: `python/spinlab/retroarch/orchestrator.py`
- Test: `tests/unit/test_retroarch_orchestrator.py`

Replaces `_unsupported_phase_e` for `ReplayCmd` and `ReplayStopCmd`. Reuses the existing `ReplayStartedEvent`/`ReplayProgressEvent`/`ReplayFinishedEvent` protocol — no schema changes.

- [ ] **Step 9.1: Write failing unit tests.**

Append to `tests/unit/test_retroarch_orchestrator.py`:

```python
from spinlab.protocol import ReplayCmd, ReplayStopCmd


class FakeMoviePlayer:
    def __init__(self):
        self.played: Path | None = None
        self.stopped: bool = False

    def play(self, src: Path) -> None:
        self.played = src

    def stop(self) -> None:
        self.stopped = True

    def is_playing(self) -> bool:
        return self.played is not None and not self.stopped


def _build_orch_with_player(player):
    return RetroArchOrchestrator(
        client=FakeNCI(),
        state_io=FakeStateIO(),
        poller=FakePoller(),
        conditions=None,
        practice_timing=None,
        speed_run_timing=None,
        movie_recorder=None,
        movie_player=player,
    )


@pytest.fixture
def orchestrator_with_fake_player():
    p = FakeMoviePlayer()
    return _build_orch_with_player(p), p


@pytest.mark.asyncio
async def test_on_replay_translates_spinrec_path_to_replay(orchestrator_with_fake_player):
    """Dashboard resolves ref_id to a .spinrec path; orchestrator translates."""
    orch, fake_player = orchestrator_with_fake_player
    await orch._on_replay(ReplayCmd(path="/data/game/rec/refid.spinrec", speed=0))
    assert fake_player.played == Path("/data/game/rec/refid.replay")


@pytest.mark.asyncio
async def test_on_replay_stop_calls_player_stop(orchestrator_with_fake_player):
    orch, fake_player = orchestrator_with_fake_player
    await orch._on_replay(ReplayCmd(path="/x.replay", speed=0))
    await orch._on_replay_stop(ReplayStopCmd())
    assert fake_player.stopped


@pytest.mark.asyncio
async def test_on_replay_without_player_raises_backend_not_implemented():
    from spinlab.errors import BackendNotImplementedError
    orch = _build_orch_with_player(None)
    with pytest.raises(BackendNotImplementedError):
        await orch._on_replay(ReplayCmd(path="/x.replay", speed=0))
```

- [ ] **Step 9.2: Run tests, expect failures.**

```bash
python -m pytest tests/unit/test_retroarch_orchestrator.py -v -k "replay"
```

Expected: failures (`unexpected keyword 'movie_player'` and missing `_on_replay`).

- [ ] **Step 9.3: Add `movie_player` to `RetroArchOrchestrator.__init__`.**

In `python/spinlab/retroarch/orchestrator.py`, add to `__init__`:

```python
def __init__(
    self,
    client,
    state_io,
    poller,
    conditions,
    practice_timing,
    speed_run_timing,
    movie_recorder=None,
    movie_player=None,
):
    # ... existing body ...
    self._movie_player = movie_player
```

- [ ] **Step 9.4: Replace dispatch entries for `ReplayCmd`/`ReplayStopCmd`.**

In the `__init__` body, in the `self._dispatch` dict, change:

```python
ReplayCmd: self._unsupported_phase_e,
ReplayStopCmd: self._unsupported_phase_e,
```

to:

```python
ReplayCmd: self._on_replay,
ReplayStopCmd: self._on_replay_stop,
```

- [ ] **Step 9.5: Add the new handler methods.**

After `_on_reference_stop`, add. **Also extend the imports at the top of the file**: add `ReplayStartedEvent` and `ReplayFinishedEvent` to the existing `from spinlab.protocol import ...` block.

```python
async def _on_replay(self, cmd: ReplayCmd) -> None:
    """Start movie playback. cmd.path is the .spinrec path the dashboard
    resolved from the ref_id (the route layer is shared with the Mesen
    backend); we translate the suffix to .replay to find the RA-side fixture.

    Emits ReplayStartedEvent synthetically — the session manager's
    _handle_replay_started drives dashboard mode transitions, and the
    poller doesn't observe replay lifecycle (only memory state). frame_count
    comes from a sibling .json metadata file when present, else 0.
    """
    if self._movie_player is None:
        from spinlab.errors import BackendNotImplementedError
        logger.warning("RetroArchOrchestrator: ReplayCmd rejected — no movie player configured")
        raise BackendNotImplementedError()
    replay_path = Path(cmd.path).with_suffix(".replay")
    await asyncio.to_thread(self._movie_player.play, replay_path)

    # Resolve frame count from sibling metadata if present.
    frame_count = 0
    meta_path = replay_path.with_suffix(".json")
    if meta_path.exists():
        try:
            import json as _json
            frame_count = int(_json.loads(meta_path.read_text()).get("frame_count", 0))
        except Exception as exc:
            logger.warning("Could not read frame_count from %s: %s", meta_path, exc)

    self.on_poller_event(ReplayStartedEvent(path=str(replay_path), frame_count=frame_count))
    logger.info("movie replay started: %s (frames=%d)", replay_path, frame_count)


async def _on_replay_stop(self, cmd: ReplayStopCmd) -> None:
    """Stop movie playback and emit ReplayFinishedEvent. Idempotent."""
    if self._movie_player is None or not self._movie_player.is_playing():
        return
    await asyncio.to_thread(self._movie_player.stop)
    self.on_poller_event(ReplayFinishedEvent())
    logger.info("movie replay stopped")
```

- [ ] **Step 9.6: Update `build_orchestrator` to construct and pass `MoviePlayer`.**

In `build_orchestrator`, after the `MovieRecorder` construction, add:

```python
from spinlab.retroarch.movie import MoviePlayer

movie_player = MoviePlayer(client=client, movie_dir=movie_dir) if movie_dir is not None else None
```

And add `movie_player=movie_player` to the `RetroArchOrchestrator(...)` call.

- [ ] **Step 9.7: Delete or rename `_unsupported_phase_e`.**

Search `python/spinlab/retroarch/orchestrator.py` for any remaining references to `_unsupported_phase_e`. With `ReplayCmd` and `ReplayStopCmd` rewired, there should be no callers left. Delete the method.

```bash
grep -n "_unsupported_phase_e" python/spinlab/retroarch/orchestrator.py
```

Expected: no matches after deletion. If matches remain, those callers need rewiring — investigate before proceeding.

- [ ] **Step 9.8: Run unit tests.**

```bash
python -m pytest tests/unit/test_retroarch_orchestrator.py -v -k "replay"
```

Expected: 3 passes.

- [ ] **Step 9.9: Run full suite.**

```bash
python -m pytest
```

Expected: all green.

- [ ] **Step 9.10: Commit.**

```bash
git add python/spinlab/retroarch/orchestrator.py tests/unit/test_retroarch_orchestrator.py
git commit -m "feat(retroarch): wire MoviePlayer into ReplayCmd/ReplayStopCmd

Replaces _unsupported_phase_e — ReplayCmd no longer raises 501. Player
is constructed in build_orchestrator alongside the recorder, sharing
movie_dir discovery. ReplayStartedEvent/ReplayProgressEvent/
ReplayFinishedEvent flow unchanged: poller sees the playback memory
naturally and emits events through the existing handler chain.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 10: Add `MoviePlayer` unit tests

**Files:**
- Create: `tests/unit/test_movie_player.py`

Coverage for the player's lifecycle and error paths against a fake NCI client + tmp filesystem. Mirrors the recorder unit-test pattern from Task 3.

- [ ] **Step 10.1: Write the unit tests.**

Create `tests/unit/test_movie_player.py`:

```python
"""Unit tests for MoviePlayer against a fake NCI client + tmp filesystem."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from spinlab.retroarch.movie import MoviePlayer


@dataclass
class FakePlayerNCI:
    calls: list[str] = field(default_factory=list)

    def play_replay(self) -> None:
        self.calls.append("play_replay")

    def halt_replay(self) -> None:
        self.calls.append("halt_replay")

    def get_status(self):
        return type("S", (), {"state": "PLAYING"})()


def test_player_starts_idle(tmp_path):
    player = MoviePlayer(client=FakePlayerNCI(), movie_dir=tmp_path)
    assert not player.is_playing()


def test_play_copies_file_into_movie_dir_and_triggers(tmp_path):
    src = tmp_path / "source.replay"
    src.write_bytes(b"RPLYcontent")
    movie_dir = tmp_path / "movies"
    fake = FakePlayerNCI()
    player = MoviePlayer(client=fake, movie_dir=movie_dir)
    player.play(src)
    staged = movie_dir / "spinlab_replay.replay"
    assert staged.exists()
    assert staged.read_bytes() == b"RPLYcontent"
    assert fake.calls == ["play_replay"]
    assert player.is_playing()


def test_stop_halts_and_unstages(tmp_path):
    src = tmp_path / "source.replay"
    src.write_bytes(b"x")
    movie_dir = tmp_path / "movies"
    fake = FakePlayerNCI()
    player = MoviePlayer(client=fake, movie_dir=movie_dir)
    player.play(src)
    player.stop()
    assert fake.calls == ["play_replay", "halt_replay"]
    assert not (movie_dir / "spinlab_replay.replay").exists()
    assert not player.is_playing()


def test_stop_is_idempotent_when_not_playing(tmp_path):
    player = MoviePlayer(client=FakePlayerNCI(), movie_dir=tmp_path)
    player.stop()  # no-op, no error


def test_play_raises_if_already_playing(tmp_path):
    src = tmp_path / "x.replay"
    src.write_bytes(b"x")
    player = MoviePlayer(client=FakePlayerNCI(), movie_dir=tmp_path)
    player.play(src)
    with pytest.raises(RuntimeError, match="already playing"):
        player.play(src)


def test_play_raises_if_source_missing(tmp_path):
    player = MoviePlayer(client=FakePlayerNCI(), movie_dir=tmp_path)
    with pytest.raises(FileNotFoundError):
        player.play(tmp_path / "doesnt_exist.replay")
```

- [ ] **Step 10.2: Run tests.**

```bash
python -m pytest tests/unit/test_movie_player.py -v
```

Expected: 6 passes.

- [ ] **Step 10.3: Commit.**

```bash
git add tests/unit/test_movie_player.py
git commit -m "test(retroarch): MoviePlayer unit tests against fake NCI

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 11: Port `test_replay_fixture.py` to RA + movie replay

**Files:**
- Modify: `tests/integration/conftest.py` (add `replay_ra_dashboard` fixture)
- Modify: `tests/integration/test_replay_fixture.py` (replace Mesen path with RA path)

The hardest task — full-stack: RA + dashboard + DB + replay through API. The existing test uses `replay_dashboard` (Mesen-bound). We replace it with `replay_ra_dashboard` (RA-bound).

- [ ] **Step 11.1: Read the existing `replay_dashboard` fixture to understand the shape.**

Open `tests/integration/conftest.py` and study `replay_dashboard` (around line 583) — note the (a) Mesen subprocess setup, (b) dashboard server on a thread, (c) wait-for-connect-and-game-id loop, (d) yield `(base_url, db, tmp_path)`.

- [ ] **Step 11.2: Add `replay_ra_dashboard` fixture to `conftest.py`.**

In `tests/integration/conftest.py`, after the existing `replay_dashboard` fixture, add:

```python
@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def replay_ra_dashboard(ra_harness):
    """Start a dashboard with RA backend connected to ra_harness's RetroArch.

    Yields (base_url, db, tmp_path). Distinct from replay_dashboard, which is
    Mesen-bound. Both can coexist during the migration window — they use
    different emulator backends and different ports.
    """
    from spinlab.config import (
        AppConfig, EmulatorConfig, NetworkConfig, PracticeConfig,
    )
    from spinlab.dashboard import create_app
    from spinlab.db import Database

    tmp = tempfile.mkdtemp(prefix="spinlab_replay_ra_")
    tmp_path = Path(tmp)

    db = Database(str(tmp_path / "spinlab.db"))
    dashboard_port = _free_port()

    rom_dir = Path(_love_yourself_rom).parent if _love_yourself_rom else None

    # The ra_harness already owns a launched RetroArch process. Point the
    # dashboard at the same NCI port (default 55355).
    config = AppConfig(
        network=NetworkConfig(
            host="127.0.0.1",
            port=15482,  # unused under RA backend
            dashboard_port=dashboard_port,
            nci_port=55355,
        ),
        emulator=EmulatorConfig(
            backend="retroarch",
            retroarch_path=Path("dummy"),  # ra_harness already launched RA
            ra_core_path=Path("dummy"),
            savestate_dir=tmp_path / "states",
            spinlab_state_dir=tmp_path / "spinlab_states",
            ra_movie_dir=tmp_path / "movies",
        ),
        data_dir=tmp_path,
        rom_dir=rom_dir,
        practice=PracticeConfig(),
    )

    app = create_app(db=db, config=config)

    uvi_config = uvicorn.Config(
        app, host="127.0.0.1", port=dashboard_port, log_level="warning",
    )
    server = uvicorn.Server(uvi_config)

    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    base_url = f"http://127.0.0.1:{dashboard_port}"
    for _ in range(40):
        try:
            resp = http_requests.get(f"{base_url}/api/state", timeout=1)
            if resp.status_code == 200:
                break
        except http_requests.ConnectionError:
            pass
        await asyncio.sleep(0.25)
    else:
        pytest.fail("Replay RA dashboard did not start within 10 seconds")

    # Wait for RA-side connect + game_id resolution
    for _ in range(40):
        resp = http_requests.get(f"{base_url}/api/state", timeout=2)
        state = resp.json()
        if state.get("tcp_connected") and state.get("game_id"):
            break
        await asyncio.sleep(0.25)
    else:
        pytest.fail("Replay RA dashboard did not connect to RA within 10 seconds")

    yield base_url, db, tmp_path

    server.should_exit = True
    thread.join(timeout=5)
    db.close()
    import shutil as _shutil_cleanup
    _shutil_cleanup.rmtree(tmp, ignore_errors=True)
```

The fixture takes `ra_harness` as a dependency to ensure RA is up before the dashboard tries to connect. RA stays alive across both fixtures because both are session-scoped.

- [ ] **Step 11.3: Replace `test_replay_fixture.py` with the RA-bound port.**

Open `tests/integration/test_replay_fixture.py`. Replace the entire file contents with:

```python
"""Full-stack replay fixture test: replay a recorded one-level run through
headless RetroArch and verify the capture pipeline produces correct segments
and save states.

Requires: RetroArch + Love Yourself ROM + tests/fixtures/love_yourself/one_level.replay
(see Task 6 of phase-e plan for fixture creation).

Replaces the Mesen-bound .spinrec version of this test as part of Phase E.
"""
from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

import pytest
import requests

from tests.integration.conftest import LOVE_YOURSELF_GAME_ID, skip_no_love_yourself

pytestmark = [pytest.mark.emulator, skip_no_love_yourself]

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "love_yourself"
FIXTURE_REPLAY = FIXTURE_DIR / "one_level.replay"
FIXTURE_META = FIXTURE_DIR / "one_level.json"

REPLAY_TIMEOUT_S = 60
POLL_INTERVAL_S = 0.5


def _api(base_url: str, method: str, path: str, **kwargs):
    return getattr(requests, method)(base_url + path, timeout=5, **kwargs)


def _wait_for_replay_mode(base_url: str, timeout: float = 10.0) -> dict:
    deadline = time.monotonic() + timeout
    last_state = None
    while time.monotonic() < deadline:
        resp = _api(base_url, "get", "/api/state")
        state = resp.json()
        last_state = state
        replay = state.get("replay")
        if state["mode"] == "replay" and replay and replay.get("total", 0) > 0:
            return state
        time.sleep(POLL_INTERVAL_S)
    pytest.fail(
        f"Mode never reached 'replay' (with frame total) within {timeout}s. "
        f"Last state: {last_state}"
    )


def _wait_for_idle_with_progress(
    base_url: str, timeout: float = REPLAY_TIMEOUT_S,
) -> tuple[dict, float, int]:
    deadline = time.monotonic() + timeout
    start = time.monotonic()
    max_frame = 0
    last_state = None
    while time.monotonic() < deadline:
        resp = _api(base_url, "get", "/api/state")
        state = resp.json()
        last_state = state
        replay = state.get("replay")
        if replay and replay.get("frame", 0) > max_frame:
            max_frame = replay["frame"]
        if state["mode"] == "idle":
            return state, time.monotonic() - start, max_frame
        time.sleep(POLL_INTERVAL_S)
    pytest.fail(
        f"Replay did not finish within {timeout}s. "
        f"Last state: mode={last_state.get('mode') if last_state else 'unknown'}"
    )


@pytest.mark.skipif(
    not FIXTURE_REPLAY.exists(),
    reason=f"movie fixture not recorded yet: {FIXTURE_REPLAY} (see Task 6 of phase-e plan)",
)
class TestReplayFixture:
    """Replay a one-level Love Yourself recording and verify capture output."""

    @pytest.fixture(autouse=True)
    def _setup(self, replay_ra_dashboard):
        base_url, db, tmp_path = replay_ra_dashboard
        self.base_url = base_url
        self.db = db
        self.tmp_path = tmp_path

        meta = json.loads(FIXTURE_META.read_text())
        self.expected_frames = meta["frame_count"]
        self.expected_segments = meta["expected_segments"]

        game_rec_dir = tmp_path / LOVE_YOURSELF_GAME_ID / "rec"
        game_rec_dir.mkdir(parents=True, exist_ok=True)
        self.ref_id = "fixture_phase_e"
        # Copy the movie file under the ref_id name expected by the replay path.
        shutil.copy2(FIXTURE_REPLAY, game_rec_dir / f"{self.ref_id}.replay")
        # The orchestrator's _on_replay reads a sibling .json for frame_count
        # to populate ReplayStartedEvent.
        shutil.copy2(FIXTURE_META, game_rec_dir / f"{self.ref_id}.json")
        # Optional: copy a paired .mss if the fixture includes one (anchor state).
        mss_fixture = FIXTURE_DIR / "one_level.mss"
        if mss_fixture.exists():
            shutil.copy2(mss_fixture, game_rec_dir / f"{self.ref_id}.mss")

    def test_replay_produces_segments(self):
        state = _api(self.base_url, "get", "/api/state").json()
        assert state["game_id"] == LOVE_YOURSELF_GAME_ID, (
            f"Game ID mismatch: expected {LOVE_YOURSELF_GAME_ID}, got {state['game_id']}"
        )

        resp = _api(self.base_url, "post", "/api/replay/start",
                    json={"ref_id": self.ref_id, "speed": 0})
        assert resp.status_code == 200, f"replay start failed: {resp.text}"

        replay_state = _wait_for_replay_mode(self.base_url)
        replay = replay_state.get("replay")
        assert replay is not None
        # Frame count varies by recording — just assert it's non-trivially large.
        assert replay.get("total", 0) > 100, (
            f"Expected replay.total > 100 frames, got {replay.get('total')}"
        )

        idle_state, elapsed_s, max_frame = _wait_for_idle_with_progress(self.base_url)

        assert max_frame > 0, "No replay frame progress observed"
        assert elapsed_s < REPLAY_TIMEOUT_S, (
            f"Replay took {elapsed_s:.1f}s — expected under {REPLAY_TIMEOUT_S}s"
        )

        resp = _api(self.base_url, "post", "/api/reference/finalize",
                    json={"name": "Phase E replay fixture"})
        assert resp.status_code == 200, f"finalize failed: {resp.text}"

        refs = _api(self.base_url, "get", "/api/references").json()["references"]
        assert len(refs) == 1, f"Expected 1 reference, got {len(refs)}"

        resp = _api(self.base_url, "get", "/api/segments")
        assert resp.status_code == 200
        segments = resp.json()["segments"]
        assert len(segments) == self.expected_segments, (
            f"Expected {self.expected_segments} segments per fixture metadata, "
            f"got {len(segments)}: "
            f"{[s.get('description', s.get('id', '?')) for s in segments]}"
        )

        # Validate segment structure — every level in the fixture should have
        # entrance→checkpoint and checkpoint→goal pairs.
        by_level: dict[int, list] = {}
        for seg in segments:
            lvl = seg["level_number"]
            by_level.setdefault(lvl, []).append(seg)

        for lvl, segs in by_level.items():
            types = [(s["start_type"], s["end_type"]) for s in segs]
            assert ("entrance", "checkpoint") in types, (
                f"Level {lvl} missing entrance->checkpoint segment"
            )
            assert ("checkpoint", "goal") in types, (
                f"Level {lvl} missing checkpoint->goal segment"
            )
```

The fixture copies the `.replay` file into the dashboard's expected `<game_id>/rec/<ref_id>.replay` location. The dashboard's `/api/replay/start` resolves `ref_id` to that file — same path the production code uses.

- [ ] **Step 11.4: Run the integration test.**

```bash
python -m pytest tests/integration/test_replay_fixture.py -v -m emulator
```

**Expected outcome 1:** test passes. Phase E (option a) is essentially done. Move to Task 12.

**Expected outcome 2:** test fails because the `replay.total` is 0 in the dashboard state. This means the synthetic `ReplayStartedEvent` from Task 9 didn't carry the right `frame_count` — likely the metadata file wasn't co-located with the `.replay` file. Verify `<ref_id>.json` exists next to `<ref_id>.replay` in the test temp dir. Update Task 11.3's `_setup` if you missed copying the metadata.

**Expected outcome 3:** test fails because segment count mismatches. Update `tests/fixtures/love_yourself/one_level.json` `expected_segments` field with the actual segment count produced.

**Expected outcome 4:** test fails because dashboard never enters `replay` mode. Task 9 already emits `ReplayStartedEvent`, so the most likely cause is that the metadata file isn't being co-located with the `.replay` fixture. Update the test setup in `_setup` to also copy `one_level.json` to `<ref_id>.json` next to the `.replay` file, so the orchestrator's metadata read finds it. If after that the test still fails to enter replay mode, trace `session_manager._handle_replay_started` to see whether the event is reaching it.

- [ ] **Step 11.5: If the test required additional `_on_replay` wiring, commit those changes too.**

```bash
git add tests/integration/test_replay_fixture.py tests/integration/conftest.py python/spinlab/retroarch/orchestrator.py
git commit -m "test(retroarch): port test_replay_fixture to RA + movie replay

Replaces Mesen+.spinrec replay fixture test with RA+.replay equivalent.
Uses one_level.replay recorded via the production reference-run path. New
replay_ra_dashboard fixture in conftest.py mirrors replay_dashboard but
points the dashboard at the ra_harness-launched RetroArch.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

- [ ] **Step 11.6: Run the test 5 consecutive times to validate no flakes (per spec DoD).**

```bash
for i in 1 2 3 4 5; do
  python -m pytest tests/integration/test_replay_fixture.py -v -m emulator
  if [ $? -ne 0 ]; then
    echo "RUN $i FAILED"
    exit 1
  fi
done
echo "5 consecutive passes ✓"
```

Expected: 5 consecutive passes. If any fail, investigate the flake before declaring DoD met.

---

## Task 12: Delete the Mesen-bound replay fixtures and cleanup

**Files:**
- Modify: `tests/integration/conftest.py` (delete `replay_mesen_process`, `replay_dashboard`)
- Delete: `tests/fixtures/love_yourself/two_level.spinrec`, `tests/fixtures/love_yourself/two_level.mss`

The Mesen-side `test_replay_fixture.py` was overwritten in Task 11. The remaining cleanup: drop the now-unused fixtures.

- [ ] **Step 12.1: Confirm no other test references the old fixtures or Mesen replay infra.**

```bash
grep -r "replay_mesen_process\|replay_dashboard\b\|two_level\.spinrec\|two_level\.mss" python/ tests/ --include="*.py" --include="*.md"
```

Expected: no matches outside of git history references. If matches exist, evaluate whether to update or delete each.

- [ ] **Step 12.2: Delete `replay_mesen_process` and `replay_dashboard` fixtures from `conftest.py`.**

In `tests/integration/conftest.py`, delete the entire `replay_mesen_process` async fixture (around line 552) and `replay_dashboard` fixture (around line 583). Also remove `replay_dashboard` from the `_collect_diagnostics` function's fixture-name iteration (around line 753) — it no longer exists.

- [ ] **Step 12.3: Delete the old fixture files.**

```bash
rm tests/fixtures/love_yourself/two_level.spinrec
rm tests/fixtures/love_yourself/two_level.mss
```

- [ ] **Step 12.4: Run the full suite one more time.**

```bash
python -m pytest
```

Expected: all green. No tests should reference the deleted fixtures.

- [ ] **Step 12.5: Commit.**

```bash
git add tests/integration/conftest.py tests/fixtures/love_yourself/
git commit -m "chore(tests): delete Mesen-bound replay fixture infra

test_replay_fixture.py ported to RA+movie replay in the previous commit. The
Mesen process fixture, dashboard fixture, and .spinrec/.mss fixture
files have no remaining callers.

Note: spinrec.py and the rest of the Mesen replay code paths (TcpManager
ReplayCmd handling, etc.) stay until Phase G — this commit only deletes
the test infrastructure that was newly orphaned by Task 11.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 13: Update migration docs

**Files:**
- Modify: `docs/retroarch-migration/status.md`
- Modify: `docs/retroarch-migration/path-to-parity.md`

Document the Phase E (option a) landing. Andrew uses these files to track migration progress; they need to reflect what works now.

- [ ] **Step 13.1: Update `status.md`.**

In `docs/retroarch-migration/status.md`:

1. In the "What works" section, add a bullet:

```markdown
- **Replay via movie (option a, 2026-05-XX).** `tests/integration/test_replay_fixture.py` runs through RA + movie replay end-to-end. `MovieRecorder` writes `.replay` alongside `.mss` during reference runs (orchestrator's `_on_reference_start`/`_stop`). `MoviePlayer` drives `ReplayCmd` via the orchestrator's `_on_replay`/`_on_replay_stop`. Smoke tests in `tests/integration/test_movie_smoke.py` cover record-toggle, playback determinism (runahead=2, secondary instance), and poller-during-playback. Fixture: `tests/fixtures/love_yourself/one_level.replay` recorded 2026-05-XX. **Awaits Andrew's smoke testing before committing to (b) — full-parity user-facing replay.**
```

2. In the "Known broken / untested" section, remove or update the bullet about replay being 501. The new state is "(a) works, (b) gated on smoke testing."

3. In "Next test pass priorities," demote replay-fixture validation since it's now covered by automated tests.

- [ ] **Step 13.2: Update `path-to-parity.md`.**

In `docs/retroarch-migration/path-to-parity.md`:

1. Update the header note (line 5):

```markdown
*The Plan 2 RA test harness landed 2026-05-08 (closes P1.2). Phase E option (a)
landed 2026-05-XX (closes part of P0.3 — replay test path works; full-parity
user-facing replay endpoint pending Andrew's smoke testing).*
```

2. Update P0.3:

```markdown
### P0.3 — Phase E: movie input recording + replay

**Status (2026-05-XX):** Option (a) shipped. `MovieRecorder` integrated into
reference-run flow; `MoviePlayer` wired to orchestrator's `ReplayCmd`. Smoke
tests cover the three foundational unknowns (control path, determinism,
polling-during-playback). `test_replay_fixture.py` ported from Mesen to RA.

**Remaining for full parity (option b):** user-facing replay endpoint
restoration, movie-by-default in dashboard, `.spinrec` → `.replay` converter
(low priority). Gated on Andrew's smoke testing of (a).
```

3. Update the "full parity" definition (line 92): item 1 changes from "✗ inputs" to "✓ inputs (movie recorded during reference runs as of 2026-05-XX)".

4. Update item 6: "Not implemented" → "Implemented (option a) 2026-05-XX, full-parity (b) pending."

- [ ] **Step 13.3: Commit.**

```bash
git add docs/retroarch-migration/status.md docs/retroarch-migration/path-to-parity.md
git commit -m "docs(retroarch): status + parity reflect Phase E option (a) landing

movie record/replay validated via test fixture path. Full-parity (option b)
remains gated on Andrew's smoke testing.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Final verification

- [ ] **Run the entire test suite.**

```bash
python -m pytest
```

Expected: all green. Per `feedback_run_all_tests.md`, this is the gate before declaring done.

- [ ] **Verify the spec's Definition of Done items, in order:**

Open [`docs/superpowers/specs/2026-05-08-phase-e-movie-replay-design.md`](../specs/2026-05-08-phase-e-movie-replay-design.md) and walk the "Definition of done" checklist. Each item should be checkable against this plan's commits:

- [ ] `test_movie_smoke.py` — three tests pass under `backend=retroarch` ← Tasks 4, 7, 8
- [ ] `MovieRecorder` integrated into the reference-run flow; `.replay` written alongside `.mss` ← Task 5
- [ ] `MoviePlayer` drives `ReplayCmd`; no longer raises `BackendNotImplementedError` ← Task 9
- [ ] `tests/fixtures/love_yourself/one_level.replay` + `one_level.json` committed ← Tasks 6, 7
- [ ] `tests/integration/test_replay_fixture.py` ported, passes 5 consecutive runs ← Task 11
- [ ] Mesen-side `test_replay_fixture.py` deleted ← Task 11/12
- [ ] `tests/unit/test_movie_recorder.py` and `test_movie_player.py` cover the recorder and player ← Tasks 3, 10
- [ ] `python -m pytest` runs clean ← This step

- [ ] **Hand off to Andrew for smoke testing.**

Per the spec, the gates from option (a) → (b) are:
- Replay-fixture test passes 5 consecutive runs (DoD above ✓)
- A real reference run on a hack other than Love Yourself produces a `.replay` file whose playback reproduces the same segment count + structure as the original capture
- Memory state at known frames is byte-identical between original capture and replay (manual probe)

Andrew runs the smoke testing in his own session, then decides whether to proceed to option (b).
