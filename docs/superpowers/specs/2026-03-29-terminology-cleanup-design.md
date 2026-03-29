# Spec: Terminology Cleanup

## Problem

SpinLab no longer uses a spaced-repetition model. The scheduler uses Kalman filters and value-of-information allocators — not SM-2 or Anki-style intervals. Four files still reference "spaced-repetition" and should be updated to describe what the system actually does.

## Changes

### 1. README.md (line 3)

**Before:** "Spaced-repetition practice for SNES romhack speedrunning."

**After:** "Intelligent practice for SNES romhack speedrunning." — rest of the sentence already describes the actual mechanism (Kalman filter, value-of-information allocator).

### 2. CLAUDE.md (line 3)

**Before:** "Spaced-repetition practice system for SNES romhack speedrunning."

**After:** "Intelligent practice system for SNES romhack speedrunning. Captures save states at segment boundaries during reference runs, serves them back in a scheduled practice loop."

### 3. DESIGN.md (line 5)

**Before:** "SpinLab turns speedrun practice into a spaced-repetition loop."

**After:** "SpinLab turns speedrun practice into a structured loop."

### 4. python/spinlab/cli.py (line 21)

**Before:** `description="SpinLab — spaced repetition practice for SNES speedrunning"`

**After:** `description="SpinLab — intelligent practice for SNES speedrunning"`

## Scope

Four single-line edits. No logic changes, no tests affected.
