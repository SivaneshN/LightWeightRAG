"""Packaged local API entry point used by the desktop application."""

from __future__ import annotations

import argparse

import uvicorn

import webapp


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the private LightWeightRAG desktop service.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()

    server = uvicorn.Server(
        uvicorn.Config(
            webapp.app,
            host=args.host,
            port=args.port,
            log_level="warning",
            access_log=False,
        )
    )
    webapp.app.state.uvicorn_server = server
    server.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
