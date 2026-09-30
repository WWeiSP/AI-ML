"""Run the studio: ``python -m studio [--host 0.0.0.0] [--port 8000]``."""
import argparse
import logging

import uvicorn

from .app import create_app


def main() -> None:
    parser = argparse.ArgumentParser(description="Agent Studio server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--db", default=None, help="SQLite path (default: $STUDIO_DB or data/studio.db)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    uvicorn.run(create_app(args.db), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
