from __future__ import annotations

import argparse
import asyncio

import uvicorn

from .app import create_api_app, create_dashboard_app
from .state import MockHermesState


async def _serve(host: str, dashboard_port: int, api_port: int) -> None:
    state = MockHermesState()
    dashboard = uvicorn.Server(
        uvicorn.Config(create_dashboard_app(state), host=host, port=dashboard_port, log_level="info")
    )
    api = uvicorn.Server(
        uvicorn.Config(create_api_app(state), host=host, port=api_port, log_level="info")
    )
    await asyncio.gather(dashboard.serve(), api.serve())


def main() -> None:
    parser = argparse.ArgumentParser(description="Run deterministic Hermes protocol mocks")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--dashboard-port", type=int, default=19119)
    parser.add_argument("--api-port", type=int, default=18642)
    args = parser.parse_args()
    asyncio.run(_serve(args.host, args.dashboard_port, args.api_port))


if __name__ == "__main__":
    main()
