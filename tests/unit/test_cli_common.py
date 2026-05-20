"""Tests for shared CLI helpers (``spinlab.cli_common``)."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from spinlab.cli_common import resolve_config_path


def test_resolve_returns_literal_path_when_exists(tmp_path, monkeypatch):
    """If the given path exists, return it without searching."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text("data_dir: ./data\n")
    monkeypatch.chdir(tmp_path)

    resolved = resolve_config_path(str(cfg))
    assert resolved == cfg


def test_resolve_walks_up_for_default_name(tmp_path, monkeypatch):
    """`config.yaml` (default) gets a parent-walk search when not in CWD."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text("data_dir: ./data\n")
    nested = tmp_path / "sub" / "nested"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)

    resolved = resolve_config_path("config.yaml")
    # Resolve both sides — the resolver canonicalizes, so the comparison
    # must too. (Tmp paths on Windows may have a `C:\Users\…\Temp\` prefix
    # that .resolve() normalizes differently from the raw Path.)
    assert resolved.resolve() == cfg.resolve()


def test_resolve_does_not_walk_up_for_explicit_non_default_path(tmp_path, monkeypatch):
    """A non-default name (e.g. `custom.yaml`) doesn't trigger the walk."""
    cfg = tmp_path / "custom.yaml"
    cfg.write_text("data_dir: ./data\n")
    nested = tmp_path / "sub"
    nested.mkdir()
    monkeypatch.chdir(nested)

    with pytest.raises(FileNotFoundError) as exc:
        resolve_config_path("custom.yaml")
    # Error message names the literal path the user passed.
    assert "custom.yaml" in str(exc.value)


def test_resolve_raises_actionable_error_when_nothing_found(tmp_path, monkeypatch):
    """The error message gives the user enough context to fix the problem
    (literal path, CWD, where we looked)."""
    monkeypatch.chdir(tmp_path)
    with pytest.raises(FileNotFoundError) as exc:
        resolve_config_path("config.yaml")
    msg = str(exc.value)
    assert "config.yaml" in msg
    assert str(tmp_path) in msg or "cwd" in msg.lower()


def test_resolve_honors_explicit_absolute_path(tmp_path):
    """An absolute path that exists is returned verbatim regardless of CWD."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text("data_dir: ./data\n")
    # Don't chdir — confirm the resolver uses the literal path.
    resolved = resolve_config_path(str(cfg.resolve()))
    assert resolved.resolve() == cfg.resolve()
