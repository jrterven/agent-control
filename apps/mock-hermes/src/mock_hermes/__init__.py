"""Deterministic test double for the Hermes dashboard and API server."""

from .app import create_api_app, create_dashboard_app
from .state import MockHermesState

__all__ = ["MockHermesState", "create_api_app", "create_dashboard_app"]
__version__ = "0.1.0"
