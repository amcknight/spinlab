# Bundle 2: RA Layer Cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Pull movie record/playback out of the 761-line `RAClient` god-facade into a focused `RAMovieIO` module that `MovieController` composes directly; inject detectors into `Poller` for testability.

**Architecture:** Strangler-fig extraction. First create `RAMovieIO` and have `RAClient` delegate internally (no API churn). Then repoint `MovieController` at `RAMovieIO` directly via `build_orchestrator`. Finally delete the now-dead delegation shims from `RAClient` and migrate the one stranded `RAClient` test. C2 piggybacks on a separate small DI tweak to `Poller`.

**Tech Stack:** Python 3.11+, asyncio, RA NCI (UDP), pytest, pyright.

---

## File Structure

**New file:**
- `python/spinlab/retroarch/movie_io.py` — `RAMovieIO` class + movie constants + movie exceptions + `MovieRecording` / `MoviePlayback` dataclasses + `_read_frame_count` helper

**Modified:**
- `python/spinlab/retroarch/raclient.py` — delete movie methods after delegation phase; keep `RAClientError` / `StateLoadError` / `StateSaveTimeoutError` / `NotReachableError` (state-side only); remove `ra_movie_dir` / `ra_log_dir` from `RAClient.__init__` (moves to `RAMovieIO` ctor)
- `python/spinlab/retroarch/movies.py` — `MovieController` takes `movie_io: RAMovieIO` and `raclient: RAClient` (for `fast_forward_toggle`); record/play calls re-route through `movie_io`
- `python/spinlab/retroarch/wiring.py` — `build_orchestrator` constructs `RAMovieIO` and threads it into `MovieController`
- `python/spinlab/retroarch/poller.py` — `Poller.__init__` accepts optional `detector` and `cold_fill` for DI

**Test changes:**
- `tests/fakes/raclient.py` — drop `record_movie`/`play_movie`/`last_recording`/`last_playback` from `FakeRAClient`; add a separate `FakeMovieIO` class in the same module
- `tests/unit/retroarch/test_movies.py` — fixture uses `FakeMovieIO` for movie calls, keeps `FakeRAClient` for `fast_forward_toggle`
- `tests/unit/retroarch/test_movie_io.py` — **new file** for `RAMovieIO` direct tests; includes the migrated "Movie source not found" test
- `tests/unit/retroarch/test_raclient.py` — delete the "Movie source not found" test (moved); leave state-side tests alone
- `tests/unit/retroarch/test_poller.py` — add detector-injection test

---

## Conventions

- TDD where there's a behavioral assertion to make. Pure-mechanical moves (copy code between files) verify via existing tests.
- One task = one commit.
- Run `python -m pytest -m "not emulator"` after each task that touches production code.
- Final task runs the full `python -m pytest` suite.

---

## Phase 1: A2 — Extract movies into RAMovieIO

### Task 1: Create `movie_io.py` and have `RAClient` delegate

**Files:**
- Create: `python/spinlab/retroarch/movie_io.py`
- Modify: `python/spinlab/retroarch/raclient.py`

The strangler-fig step: lift the movie code into a new module, have `RAClient` instantiate `RAMovieIO` internally and delegate its public `record_movie` / `play_movie` to it. No external API changes; existing tests should pass unchanged.

- [ ] **Step 1: Create `python/spinlab/retroarch/movie_io.py`**

Write the new module. The class wraps NCI directly (it doesn't need RAClient), but takes a `game_basename` getter callable because the basename changes after `connect()` and `RAMovieIO` shouldn't reach into `RAClient` for it.

```python
"""RAMovieIO — RA movie record/playback over NCI.

Extracted from RAClient. The orchestrator's MovieController composes this
object directly; RAClient holds an internal instance and delegates its
public record_movie / play_movie to it for the transition window before
those wrappers go away.

The class wraps NCI directly. Construction needs:
  - the NCI client (for record/halt/play_replay + read_ram during verify)
  - movie_dir: where RA writes .replay files (None disables this object)
  - log_dir: RA's logs dir, for replay-slot resolution (None means slot 0)
  - game_basename: callable that returns the current ROM basename (changes
    after RAClient.connect, so it's a getter rather than a captured string)

Errors raised here are MovieRecordError / MoviePlaybackError. StateLoadError
is imported from raclient and reused for "source file missing" — semantically
a load-side error, kept for test backward compat.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

from spinlab.retroarch.exceptions import NCIError
from spinlab.retroarch.nci import NCIClient

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

# Movie record/play file-stability polling.
MOVIE_POLL_INTERVAL_SEC = 0.2
MOVIE_POLL_ATTEMPTS = 5

# Movie playback verification window: sample WRAM, sleep, sample again. If
# both samples match, RA never advanced a frame — it refused the file.
# 150ms ≈ 9 frames at 60Hz; reliably catches a stuck emulator.
PLAYBACK_VERIFY_SLEEP_SEC = 0.15
PLAYBACK_VERIFY_BYTES = 16

# Pattern for parsing RA's log lines that report the current replay slot.
# RA emits in three forms:
#   - At startup:                "[Replay] Found last replay slot: #N"
#   - After SLOT_PLUS/MINUS:     "[Replay] Replay slot: N"
#   - When recording starts:     "[Replay] Starting movie record to "<path>.replay<N>""
# The third match is critical: replay_auto_index="true" bumps the slot at
# record-start without emitting a "Replay slot:" line. Without it the parser
# misses the auto-index bump and stages at the stale pre-record slot.
_REPLAY_SLOT_LOG_PATTERN = re.compile(
    r"\[Replay\] (?:"
    r"Replay slot: |"
    r"Found last replay slot: #|"
    r'Starting movie record to "[^"]*\.replay'
    r")(\d+)"
)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class MovieIOError(Exception):
    """Base error for RAMovieIO operations."""


class MovieRecordError(MovieIOError):
    """RA produced no new or rewritten movie file after HALT_REPLAY."""


class MoviePlaybackError(MovieIOError):
    """RA refused to load the movie file — no WRAM advance after PLAY_REPLAY."""


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------

@dataclass
class MovieRecording:
    """Handle for an in-progress recording. Caller invokes ``.stop()`` to finish."""

    path: Path
    _stop: Callable[[], Awaitable[Path]] = field(repr=False)

    async def stop(self) -> Path:
        return await self._stop()


@dataclass
class MoviePlayback:
    """Handle for in-progress playback. Caller invokes ``.stop()`` to halt."""

    path: Path
    frame_count: int
    _stop: Callable[[], Awaitable[None]] = field(repr=False)

    async def stop(self) -> None:
        await self._stop()


# ---------------------------------------------------------------------------
# RAMovieIO
# ---------------------------------------------------------------------------

class RAMovieIO:
    """Movie record/playback over RA's NCI.

    Construct with a configured NCI client, the movie dir RA writes to, an
    optional log dir for replay-slot resolution, and a callable returning
    the current ROM basename (which changes after RAClient.connect).
    """

    def __init__(
        self,
        nci: NCIClient,
        movie_dir: Path | None,
        log_dir: Path | None,
        game_basename: Callable[[], str | None],
    ) -> None:
        self._nci = nci
        self._movie_dir = movie_dir
        self._log_dir = log_dir
        self._game_basename = game_basename

    async def record_movie(self, dest_path: Path) -> MovieRecording:
        """Fire RECORD_REPLAY and return a handle whose ``.stop()`` halts RA
        and copies the resulting .replay file to ``dest_path``.
        """
        from spinlab.retroarch.raclient import RAClientError
        if self._movie_dir is None:
            raise RAClientError(
                "record_movie called but movie_dir is not configured — "
                "set it at RAMovieIO construction."
            )
        movie_dir = self._movie_dir
        movie_dir.mkdir(parents=True, exist_ok=True)

        baseline = {
            f: f.stat().st_mtime
            for f in movie_dir.iterdir()
            if f.is_file()
        }
        await asyncio.to_thread(self._nci.record_replay)
        logger.info(
            'record_movie start dest="%s" baseline_files=%d',
            dest_path, len(baseline),
        )

        async def _stop() -> Path:
            return await asyncio.to_thread(self._stop_recording, dest_path, baseline)

        return MovieRecording(path=dest_path, _stop=_stop)

    def _stop_recording(self, dest_path: Path, baseline: dict[Path, float]) -> Path:
        assert self._movie_dir is not None
        movie_dir = self._movie_dir
        self._nci.halt_replay()

        # Two-phase poll: find the changed file, then wait for its size+mtime
        # to STABILIZE for a full poll interval before copying. RA's
        # halt_replay is fire-and-forget — without the stability check we
        # catch the file mid-flush and end up with a truncated .replay.
        changed: Path | None = None
        for _ in range(MOVIE_POLL_ATTEMPTS):
            for f in movie_dir.iterdir():
                if not f.is_file():
                    continue
                baseline_mt = baseline.get(f)
                cur_mt = f.stat().st_mtime
                if baseline_mt is None or cur_mt > baseline_mt:
                    if changed is None or cur_mt > changed.stat().st_mtime:
                        changed = f
            if changed is not None:
                break
            time.sleep(MOVIE_POLL_INTERVAL_SEC)

        if changed is not None:
            last_size = changed.stat().st_size
            last_mt = changed.stat().st_mtime
            for _ in range(MOVIE_POLL_ATTEMPTS):
                time.sleep(MOVIE_POLL_INTERVAL_SEC)
                cur = changed.stat()
                if cur.st_size == last_size and cur.st_mtime == last_mt:
                    break
                last_size = cur.st_size
                last_mt = cur.st_mtime

        if changed is None:
            existing_replays = sum(
                1 for f in baseline if f.suffix.startswith(".replay")
            )
            hint = ""
            if existing_replays > 0:
                hint = (
                    f" — {existing_replays} existing .replay* file(s) in dir; "
                    'this often means retroarch.cfg has replay_max_keep=0. '
                    'Set replay_max_keep = "99" to fix.'
                )
            logger.warning(
                'record_movie no_new_file dir="%s" attempts=%d%s',
                movie_dir, MOVIE_POLL_ATTEMPTS, hint,
            )
            raise MovieRecordError(
                f"No new or rewritten movie file appeared in {movie_dir} "
                f"after {MOVIE_POLL_ATTEMPTS} polls{hint}"
            )

        dest_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(changed), str(dest_path))
        for attempt in range(MOVIE_POLL_ATTEMPTS):
            try:
                os.unlink(changed)
                break
            except PermissionError:
                if attempt < MOVIE_POLL_ATTEMPTS - 1:
                    time.sleep(MOVIE_POLL_INTERVAL_SEC)
        logger.info(
            'record_movie stop src="%s" dest="%s"', changed.name, dest_path,
        )
        return dest_path

    async def play_movie(self, src_path: Path) -> MoviePlayback:
        """Stage ``src_path`` at RA's current runtime slot, fire PLAY_REPLAY,
        verify by sampling WRAM advances.
        """
        from spinlab.retroarch.raclient import RAClientError, StateLoadError
        if self._movie_dir is None:
            raise RAClientError(
                "play_movie called but movie_dir is not configured."
            )
        src = Path(src_path)
        if not src.exists():
            raise StateLoadError(f"Movie source not found: {src}")

        target_slot = self._find_current_replay_slot() or 0
        basename = self._game_basename()
        staged = self._movie_dir / f"{basename}.replay{target_slot}"
        await asyncio.to_thread(self._stage_and_play, src, staged)

        if not await self._verify_playback_advanced():
            await asyncio.to_thread(self._nci.halt_replay)
            logger.warning(
                'play_movie verify_failed src="%s" no_wram_advance_within_ms=%d',
                src, int(PLAYBACK_VERIFY_SLEEP_SEC * 1000),
            )
            raise MoviePlaybackError(
                f"RA refused to load {src.name} (likely ROM-checksum "
                "mismatch or unreadable file). Check RA's log for the "
                "underlying error."
            )

        frame_count = _read_frame_count(src)

        logger.info(
            'play_movie start src="%s" slot=%d frame_count=%d',
            src, target_slot, frame_count,
        )

        async def _stop() -> None:
            await asyncio.to_thread(self._stop_playback, staged)

        return MoviePlayback(path=src, frame_count=frame_count, _stop=_stop)

    def _stage_and_play(self, src: Path, staged: Path) -> None:
        staged.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(src), str(staged))
        self._nci.play_replay()

    def _stop_playback(self, staged: Path) -> None:
        try:
            self._nci.halt_replay()
        finally:
            if staged.exists():
                try:
                    staged.unlink()
                except OSError as exc:
                    logger.warning(
                        'play_movie stop unlink_failed staged="%s" err=%s',
                        staged, exc,
                    )
        logger.info('play_movie stop staged="%s"', staged)

    def _find_current_replay_slot(self) -> int | None:
        if self._log_dir is None or not self._log_dir.exists():
            return None
        logs = sorted(
            self._log_dir.glob("retroarch__*.log"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if not logs:
            return None
        try:
            text = logs[0].read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            logger.warning('play_movie log_read_failed path="%s" err=%s', logs[0], exc)
            return None
        matches = _REPLAY_SLOT_LOG_PATTERN.findall(text)
        if not matches:
            return None
        return int(matches[-1])

    async def _verify_playback_advanced(self) -> bool:
        try:
            before = await asyncio.to_thread(
                self._nci.read_ram, 0x0000, PLAYBACK_VERIFY_BYTES,
            )
            await asyncio.sleep(PLAYBACK_VERIFY_SLEEP_SEC)
            after = await asyncio.to_thread(
                self._nci.read_ram, 0x0000, PLAYBACK_VERIFY_BYTES,
            )
        except NCIError as exc:
            logger.warning('play_movie verify_read_failed err=%s', exc)
            return False
        return before != after


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _read_frame_count(movie_path: Path) -> int:
    """Read frame_count from the sibling ``<movie>.json`` metadata file."""
    meta = movie_path.with_suffix(".json")
    if not meta.exists():
        return 0
    try:
        return int(json.loads(meta.read_text()).get("frame_count", 0))
    except Exception as exc:
        logger.warning('frame_count_read_failed path="%s" err=%s', meta, exc)
        return 0
```

- [ ] **Step 2: Update `raclient.py` to delegate to `RAMovieIO`**

In `python/spinlab/retroarch/raclient.py`:

(a) Add an import near the top (after the existing imports):

```python
from spinlab.retroarch.movie_io import (
    MoviePlayback,
    MoviePlaybackError,
    MovieRecordError,
    MovieRecording,
    RAMovieIO,
)
```

(b) Delete the movie-tunables block (lines 66-90 — `MOVIE_POLL_INTERVAL_SEC`, `MOVIE_POLL_ATTEMPTS`, `PLAYBACK_VERIFY_SLEEP_SEC`, `PLAYBACK_VERIFY_BYTES`, `_REPLAY_SLOT_LOG_PATTERN`).

(c) Delete the movie exception classes (lines ~113-118 — `MovieRecordError`, `MoviePlaybackError`). Keep `RAClientError`, `NotReachableError`, `StateSaveTimeoutError`, `StateLoadError`.

(d) Delete the movie dataclasses block (`MovieRecording`, `MoviePlayback`).

(e) Delete the `_read_frame_count` helper function at the bottom of the file.

(f) In `RAClient.__init__`, after the existing assignments, add construction of the internal `RAMovieIO`:

```python
        self._movie_io = RAMovieIO(
            nci=self._nci,
            movie_dir=self._ra_movie_dir,
            log_dir=self._ra_log_dir,
            game_basename=lambda: self._game_basename,
        )
```

(g) Replace the bodies of `record_movie` and `play_movie` and delete `_stop_recording`, `_stage_and_play`, `_stop_playback`, `_find_current_replay_slot`, `_verify_playback_advanced`. The `record_movie` / `play_movie` shims become:

```python
    async def record_movie(self, dest_path: Path) -> MovieRecording:
        return await self._movie_io.record_movie(dest_path)

    async def play_movie(self, src_path: Path) -> MoviePlayback:
        return await self._movie_io.play_movie(src_path)
```

(h) Add a property on `RAClient` so callers like `MovieController` and `build_orchestrator` can reach the underlying movie IO directly when they need to (used in Task 2):

```python
    @property
    def movie_io(self) -> RAMovieIO:
        return self._movie_io
```

- [ ] **Step 3: Run all retroarch tests**

```bash
python -m pytest tests/unit/retroarch/ -v
```

Expected: PASS.

- [ ] **Step 4: Run the full fast suite**

```bash
python -m pytest -m "not emulator"
```

Expected: PASS (859+ tests).

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/retroarch/movie_io.py python/spinlab/retroarch/raclient.py
git commit -m "retroarch: extract RAMovieIO; RAClient delegates record/play_movie"
```

---

### Task 2: Repoint `MovieController` and `build_orchestrator` at `RAMovieIO`

**Files:**
- Modify: `python/spinlab/retroarch/movies.py`
- Modify: `python/spinlab/retroarch/wiring.py`
- Modify: `tests/fakes/raclient.py` — add `FakeMovieIO`
- Modify: `tests/unit/retroarch/test_movies.py` — fixtures use `FakeMovieIO`

After Task 1, `MovieController` still goes through `RAClient.record_movie` / `play_movie` (which now delegate). This task gives `MovieController` direct access to `RAMovieIO`, leaving `RAClient` purely for `fast_forward_toggle` from the controller's perspective.

- [ ] **Step 1: Add `FakeMovieIO` to `tests/fakes/raclient.py`**

In `tests/fakes/raclient.py`, after the existing imports add:

```python
from spinlab.retroarch.movie_io import (
    MoviePlayback,
    MoviePlaybackError,
    MovieRecording,
)
```

(Most of these may already be imported transitively — replace the existing `from spinlab.retroarch.raclient import (...)` block to import only the things still in raclient: `ConnectInfo`, `RAHotkey`. Movie types come from `movie_io`.)

Then add this class to the file (place it AFTER `FakeRAClient`, BEFORE `FakePoller`):

```python
@dataclass
class FakeMovieIO:
    """In-process replacement for RAMovieIO. Async surface matches.

    Default behavior is happy-path: record/play return live handles whose
    ``.stop()`` is a no-op. Override per-test by setting fields after
    construction.
    """

    fail_play_movie: bool = False
    play_movie_error_message: str = "fake refusal"
    frame_count: int = 0

    def __post_init__(self) -> None:
        self.record_movie_calls: list[Path] = []
        self.play_movie_calls: list[Path] = []
        self.last_recording: MovieRecording | None = None
        self.last_playback: MoviePlayback | None = None

    async def record_movie(self, dest_path: Path) -> MovieRecording:
        path = Path(dest_path)
        self.record_movie_calls.append(path)

        async def _stop() -> Path:
            return path

        rec = MovieRecording(path=path, _stop=_stop)
        self.last_recording = rec
        return rec

    async def play_movie(self, src_path: Path) -> MoviePlayback:
        path = Path(src_path)
        self.play_movie_calls.append(path)
        if self.fail_play_movie:
            raise MoviePlaybackError(self.play_movie_error_message)

        async def _stop() -> None:
            pass

        pb = MoviePlayback(path=path, frame_count=self.frame_count, _stop=_stop)
        self.last_playback = pb
        return pb
```

Note: leave the movie methods (`record_movie`, `play_movie`, `last_recording`, `last_playback`, `fail_play_movie`, etc.) ON `FakeRAClient` for now. They become dead in Task 3.

- [ ] **Step 2: Update `MovieController` to take `movie_io`**

In `python/spinlab/retroarch/movies.py`, replace the constructor and the calls inside `start_recording` / `start_playback` to go through `movie_io`.

Replace the existing imports block:

```python
from spinlab.retroarch.raclient import (
    MoviePlayback,
    MoviePlaybackError,
    MovieRecording,
    RAClient,
    RAClientError,
)
```

with:

```python
from spinlab.retroarch.movie_io import (
    MoviePlayback,
    MoviePlaybackError,
    MovieRecording,
    RAMovieIO,
)
from spinlab.retroarch.raclient import RAClient, RAClientError
```

Replace the `__init__`:

```python
    def __init__(
        self,
        raclient: RAClient,
        enable: bool,
        on_event: Callable[[object], None],
    ) -> None:
        self._raclient = raclient
        self._enable = enable
        self._on_event = on_event
        self._active_recording: MovieRecording | None = None
        self._active_playback: MoviePlayback | None = None
        self._fast_forwarding: bool = False
```

with:

```python
    def __init__(
        self,
        movie_io: RAMovieIO,
        raclient: RAClient,
        enable: bool,
        on_event: Callable[[object], None],
    ) -> None:
        self._movie_io = movie_io
        self._raclient = raclient
        self._enable = enable
        self._on_event = on_event
        self._active_recording: MovieRecording | None = None
        self._active_playback: MoviePlayback | None = None
        self._fast_forwarding: bool = False
```

Inside `start_recording`, change:

```python
            self._active_recording = await self._raclient.record_movie(path)
```

to:

```python
            self._active_recording = await self._movie_io.record_movie(path)
```

Inside `start_playback`, change:

```python
            self._active_playback = await self._raclient.play_movie(path)
```

to:

```python
            self._active_playback = await self._movie_io.play_movie(path)
```

`fast_forward_toggle` callsites stay on `self._raclient` (unchanged).

- [ ] **Step 3: Update `build_orchestrator` to construct `RAMovieIO` and pass it to `MovieController`**

In `python/spinlab/retroarch/wiring.py`, find the existing block (around line 106):

```python
    movies = MovieController(
        raclient=raclient,
        enable=movie_dir is not None,
        on_event=lambda ev: None,  # rebound by orch.__init__
    )
```

Replace with:

```python
    movies = MovieController(
        movie_io=raclient.movie_io,
        raclient=raclient,
        enable=movie_dir is not None,
        on_event=lambda ev: None,  # rebound by orch.__init__
    )
```

`raclient.movie_io` was exposed as a property in Task 1.

- [ ] **Step 4: Update `tests/unit/retroarch/test_movies.py` fixtures**

Replace the existing `mc` fixture and the inline constructions:

```python
@pytest.fixture
def raclient():
    return FakeRAClient()


@pytest.fixture
def events():
    return []


@pytest.fixture
def mc(raclient, events):
    return MovieController(raclient=raclient, enable=True, on_event=events.append)
```

with:

```python
from tests.fakes.raclient import FakeMovieIO


@pytest.fixture
def raclient():
    return FakeRAClient()


@pytest.fixture
def movie_io():
    return FakeMovieIO()


@pytest.fixture
def events():
    return []


@pytest.fixture
def mc(movie_io, raclient, events):
    return MovieController(
        movie_io=movie_io, raclient=raclient,
        enable=True, on_event=events.append,
    )
```

Then update every inline `MovieController(raclient=raclient, ...)` construction in this file to also pass `movie_io=...`. Use grep:

```bash
grep -n "MovieController(" tests/unit/retroarch/test_movies.py
```

For each match, add `movie_io=movie_io` (or `movie_io=FakeMovieIO()` for one-off constructions inside test bodies).

Update test assertions that check `raclient.record_movie_calls` etc. to instead check `movie_io.record_movie_calls`. Same for `play_movie_calls`, `last_recording`, `last_playback`, `fail_play_movie`, `frame_count`, `play_movie_error_message`. After the migration, `raclient` is used in these tests ONLY for `fast_forward_toggles` assertions and for the now-irrelevant `raise_on_connect` etc.

Two tests need extra care because they currently monkey-patch `raclient.record_movie` / `raclient.play_movie` directly — those should now patch `movie_io.record_movie` etc.

- [ ] **Step 5: Run movie tests**

```bash
python -m pytest tests/unit/retroarch/test_movies.py -v
```

Expected: PASS.

- [ ] **Step 6: Run the full fast suite**

```bash
python -m pytest -m "not emulator"
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add python/spinlab/retroarch/movies.py python/spinlab/retroarch/wiring.py tests/fakes/raclient.py tests/unit/retroarch/test_movies.py
git commit -m "retroarch: MovieController takes RAMovieIO directly; wire it via build_orchestrator"
```

---

### Task 3: Delete the now-dead `RAClient.record_movie` / `play_movie` shims

**Files:**
- Modify: `python/spinlab/retroarch/raclient.py`
- Modify: `tests/fakes/raclient.py`
- Create: `tests/unit/retroarch/test_movie_io.py`
- Modify: `tests/unit/retroarch/test_raclient.py`

The shims added in Task 1 are no longer used (everything goes through `RAMovieIO` after Task 2). Delete them. Migrate the one orphan test from `test_raclient.py` to a new `test_movie_io.py`.

- [ ] **Step 1: Audit remaining shim usages**

```bash
grep -rn "raclient.record_movie\|raclient.play_movie\|\.record_movie(\|\.play_movie(" python/ tests/
```

Expected: zero matches against `raclient.` in production code; matches in `tests/fakes/raclient.py` and possibly older tests. Each match is a place to update.

- [ ] **Step 2: Delete the shims from `RAClient`**

In `python/spinlab/retroarch/raclient.py`, delete the methods (added in Task 1):

```python
    async def record_movie(self, dest_path: Path) -> MovieRecording:
        return await self._movie_io.record_movie(dest_path)

    async def play_movie(self, src_path: Path) -> MoviePlayback:
        return await self._movie_io.play_movie(src_path)
```

Also delete the `MoviePlayback` / `MoviePlaybackError` / `MovieRecordError` / `MovieRecording` re-imports at the top — `RAClient` no longer uses these types directly. Keep the `RAMovieIO` import only.

- [ ] **Step 3: Remove movie methods from `FakeRAClient`**

In `tests/fakes/raclient.py`, delete from `FakeRAClient`:
- `record_movie_calls` / `play_movie_calls` from `__post_init__`
- `last_recording` / `last_playback` from `__post_init__`
- `fail_play_movie`, `play_movie_error_message`, `frame_count` dataclass fields
- `record_movie()` async method
- `play_movie()` async method

Leave the `state_*`, `read_ram`, `write_ram`, `press`, `reset`, `fast_forward_toggle`, connect/disconnect methods intact.

- [ ] **Step 4: Create `tests/unit/retroarch/test_movie_io.py` with the migrated tests**

```python
"""Tests for RAMovieIO — RA movie record/playback."""
from __future__ import annotations

from pathlib import Path

import pytest

from spinlab.retroarch.movie_io import RAMovieIO
from spinlab.retroarch.raclient import StateLoadError


class _StubNCI:
    """Records NCI calls; no actual UDP. Sufficient for surface-level tests."""

    def __init__(self) -> None:
        self.record_replay_calls = 0
        self.halt_replay_calls = 0
        self.play_replay_calls = 0

    def record_replay(self) -> None:
        self.record_replay_calls += 1

    def halt_replay(self) -> None:
        self.halt_replay_calls += 1

    def play_replay(self) -> None:
        self.play_replay_calls += 1

    def read_ram(self, addr: int, length: int) -> bytes:
        return b"\x00" * length


@pytest.mark.asyncio
async def test_play_movie_raises_when_source_missing(tmp_path):
    """RAMovieIO.play_movie raises StateLoadError if the .replay file does not exist."""
    nci = _StubNCI()
    movie_io = RAMovieIO(
        nci=nci,
        movie_dir=tmp_path,
        log_dir=None,
        game_basename=lambda: "Test Game",
    )
    missing = tmp_path / "no-such.replay"
    with pytest.raises(StateLoadError, match="Movie source not found"):
        await movie_io.play_movie(missing)
```

- [ ] **Step 5: Delete the migrated test from `test_raclient.py`**

In `tests/unit/retroarch/test_raclient.py`, find and delete the test function whose assertion uses `match="Movie source not found"` (around line 374). It's now covered by `test_movie_io.py`.

Also remove any RAClient construction args related to movies in tests that previously passed `ra_movie_dir` purely for movie tests. Search and remove only the unused ones; leave any test that still needs the field intact.

- [ ] **Step 6: Run targeted tests**

```bash
python -m pytest tests/unit/retroarch/test_raclient.py tests/unit/retroarch/test_movie_io.py tests/unit/retroarch/test_movies.py -v
```

Expected: PASS.

- [ ] **Step 7: Run the full fast suite**

```bash
python -m pytest -m "not emulator"
```

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add python/spinlab/retroarch/raclient.py tests/fakes/raclient.py tests/unit/retroarch/test_movie_io.py tests/unit/retroarch/test_raclient.py
git commit -m "retroarch: delete RAClient movie shims; migrate test to test_movie_io"
```

---

### Task 4: A2 verification

- [ ] **Step 1: Audit RAClient size**

```bash
wc -l python/spinlab/retroarch/raclient.py python/spinlab/retroarch/movie_io.py python/spinlab/retroarch/movies.py
```

Expected: `raclient.py` is ~500 lines (down from 761), `movie_io.py` is ~250 lines.

- [ ] **Step 2: Confirm no production code still references movies through RAClient**

```bash
grep -rn "raclient\.\(record_movie\|play_movie\)\|RAClient.*record_movie\|RAClient.*play_movie" python/
```

Expected: zero matches.

- [ ] **Step 3: Type check the changed modules**

```bash
npx pyright python/spinlab/retroarch/raclient.py python/spinlab/retroarch/movie_io.py python/spinlab/retroarch/movies.py python/spinlab/retroarch/wiring.py
```

Expected: 0 errors, 0 warnings.

- [ ] **Step 4: Full fast suite**

```bash
python -m pytest -m "not emulator"
```

Expected: PASS.

---

## Phase 2: C2 — Inject detectors into Poller

### Task 5: Add detector + cold_fill DI to Poller

**Files:**
- Modify: `python/spinlab/retroarch/poller.py`
- Modify: `tests/unit/retroarch/test_poller.py`

`Poller.__init__` currently constructs `TransitionDetector()` and `ColdFillSpawnDetector()` inline. Allow callers to inject these so tests can pass spies / fakes. Defaults preserve existing behavior.

- [ ] **Step 1: Write the failing test**

Add to `tests/unit/retroarch/test_poller.py`:

```python
@pytest.mark.asyncio
async def test_poller_uses_injected_detectors():
    """An injected detector is the one Poller drives — DI works.

    Smoke test for C2: when callers (production or tests) want to substitute
    the transition detector, they pass a constructed instance to Poller's
    constructor, and that's the instance whose .step is called per tick.
    """
    from spinlab.retroarch.cold_fill_detector import ColdFillSpawnDetector
    from spinlab.retroarch.detector import TransitionDetector

    class _SpyDetector(TransitionDetector):
        def __init__(self) -> None:
            super().__init__()
            self.step_calls = 0

        def step(self, snapshot, timestamp_ms):
            self.step_calls += 1
            return []

    snapshots = iter([_snap(), _snap(), _snap()])
    spy = _SpyDetector()
    cold_fill = ColdFillSpawnDetector()

    deps = PollerDeps(
        client=_FakeClient(),
        read_snapshot=_make_snapshots(snapshots),
        on_event=lambda _ev: None,
    )
    poller = Poller(deps, period_sec=0.001, detector=spy, cold_fill=cold_fill)
    task = asyncio.create_task(poller.run())
    await asyncio.sleep(0.02)
    poller.stop()
    await task

    assert spy.step_calls >= 1
```

- [ ] **Step 2: Run test to verify it fails**

```bash
python -m pytest tests/unit/retroarch/test_poller.py::test_poller_uses_injected_detectors -v
```

Expected: `TypeError: __init__() got an unexpected keyword argument 'detector'`.

- [ ] **Step 3: Add DI parameters to `Poller.__init__`**

In `python/spinlab/retroarch/poller.py`, replace the existing constructor:

```python
class Poller:
    def __init__(self, deps: PollerDeps, period_sec: float = DEFAULT_PERIOD_SEC) -> None:
        self._deps = deps
        self._period = period_sec
        self._stopped = False
        self._detector = TransitionDetector()
        self._cold_fill = ColdFillSpawnDetector()
        self._start_ms = time.perf_counter() * 1000
        self._last_seen_state_version = deps.state_version()
        # Number of successful RAM reads completed (excludes polls that raised
        # an exception). Used by tests to verify throughput during playback.
        self.poll_count: int = 0
```

with:

```python
class Poller:
    def __init__(
        self,
        deps: PollerDeps,
        period_sec: float = DEFAULT_PERIOD_SEC,
        detector: TransitionDetector | None = None,
        cold_fill: ColdFillSpawnDetector | None = None,
    ) -> None:
        self._deps = deps
        self._period = period_sec
        self._stopped = False
        self._detector = detector if detector is not None else TransitionDetector()
        self._cold_fill = cold_fill if cold_fill is not None else ColdFillSpawnDetector()
        self._start_ms = time.perf_counter() * 1000
        self._last_seen_state_version = deps.state_version()
        # Number of successful RAM reads completed (excludes polls that raised
        # an exception). Used by tests to verify throughput during playback.
        self.poll_count: int = 0
```

- [ ] **Step 4: Run test to verify it passes**

```bash
python -m pytest tests/unit/retroarch/test_poller.py::test_poller_uses_injected_detectors -v
```

Expected: PASS.

- [ ] **Step 5: Run full poller suite + fast suite**

```bash
python -m pytest tests/unit/retroarch/test_poller.py -v
python -m pytest -m "not emulator"
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add python/spinlab/retroarch/poller.py tests/unit/retroarch/test_poller.py
git commit -m "poller: accept injected detector and cold_fill for testability"
```

---

### Task 6: C2 verification

- [ ] **Step 1: Confirm existing callers unaffected (defaults preserve behavior)**

```bash
grep -rn "Poller(" python/ tests/
```

Expected: existing call patterns (`Poller(deps, period_sec=...)`) still work without the new kwargs.

- [ ] **Step 2: Type check**

```bash
npx pyright python/spinlab/retroarch/poller.py
```

Expected: 0 errors.

---

## Phase 3: Final verification

### Task 7: Full suite + audit

- [ ] **Step 1: Run the full pytest suite**

```bash
python -m pytest
```

Expected: PASS (871+ tests, including emulator and frontend).

- [ ] **Step 2: Type-check the entire RA package**

```bash
npx pyright python/spinlab/retroarch/
```

Expected: no new errors versus baseline. Pre-existing errors are out of scope.

- [ ] **Step 3: Confirm file size deltas**

```bash
wc -l python/spinlab/retroarch/raclient.py python/spinlab/retroarch/movie_io.py python/spinlab/retroarch/movies.py python/spinlab/retroarch/poller.py
```

Expected: `raclient.py` reduced by ~250 lines, `movie_io.py` added at ~250 lines.

- [ ] **Step 4: Update memory**

Add a brief project memory note summarizing Bundle 2's outcome — file sizes, the new module's role, the Poller DI shape. Useful for future bundles.

---

## Out of Scope (Bundle 3)

- Replay flow: `replay_total` relocation onto `ReferenceController` (C1)
- Fix the eager mode-flip race in `start_replay` (C3)

---

## Self-Review

**Spec coverage:**
- A2-movies: Tasks 1-4 extract `RAMovieIO`, repoint `MovieController`, delete `RAClient` shims, verify size deltas.
- C2: Tasks 5-6 inject detectors with default-arg DI plus a regression test.

**Placeholder scan:** No TBDs. Each step has the actual code to paste. Step 1 of Task 2 may require minor judgment when editing fixtures (every `MovieController(...)` call in `test_movies.py` is updated identically — task is explicit about using grep to find them).

**Type consistency:**
- `RAMovieIO(nci, movie_dir, log_dir, game_basename)` — used in Tasks 1, 2 (`raclient.py` + `wiring.py`), and 5 (no usage in C2).
- `MovieController(movie_io, raclient, enable, on_event)` — kwargs-required form in Tasks 2 fixtures.
- `Poller(deps, period_sec, detector=None, cold_fill=None)` — used in Task 5.
- `FakeMovieIO` — added in Task 2, dropped FakeRAClient counterparts in Task 3.

Names match between definition (Task 1) and use (Tasks 2, 3, 5).
