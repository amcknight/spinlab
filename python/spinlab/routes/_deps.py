"""FastAPI dependency injection helpers."""
from __future__ import annotations

from fastapi import Request

from spinlab.config import AppConfig
from spinlab.db import Database
from spinlab.session_manager import SessionManager


def get_session(request: Request) -> SessionManager:
    return request.app.state.session


def get_db(request: Request) -> Database:
    return request.app.state.db


def get_config(request: Request) -> AppConfig:
    return request.app.state.config
