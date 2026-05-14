"""Dump the FastAPI app's OpenAPI schema to a JSON file.

Used by the frontend codegen pipeline (``npm run gen-types``) so types are
generated from the canonical Pydantic models defined in
``python/spinlab/api_schemas.py``. No live server required.

Usage:
    python -m scripts.dump_openapi [output_path]

Default output path is ``frontend/openapi.json`` relative to the repo root.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path


def _build_schema() -> dict:
    """Construct a throwaway FastAPI app and return its OpenAPI dict.

    Uses an in-memory-like temp DB so create_app's wiring (migrations, etc.)
    runs without touching the user's real data directory.
    """
    from spinlab.config import AppConfig, EmulatorConfig, NetworkConfig
    from spinlab.dashboard import create_app
    from spinlab.db import Database

    with tempfile.TemporaryDirectory() as td:
        db = Database(os.path.join(td, "schema_dump.db"))
        try:
            cfg = AppConfig(
                network=NetworkConfig(),
                emulator=EmulatorConfig(
                    savestate_dir=Path(td) / "ra-states",
                    spinlab_state_dir=Path(td) / "spinlab-states",
                ),
                data_dir=Path(td) / "data",
                rom_dir=None,
            )
            app = create_app(db=db, config=cfg)
            return app.openapi()
        finally:
            db.close()


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent
    out_path = (
        Path(sys.argv[1])
        if len(sys.argv) > 1
        else repo_root / "frontend" / "openapi.json"
    )
    schema = _build_schema()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(schema, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {out_path} ({len(schema.get('paths', {}))} paths)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
