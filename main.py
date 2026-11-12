"""Entry point: python main.py [--config config.yaml]"""

import argparse
import logging
import os

import uvicorn

from src.app import create_app
from src.settings import load_settings


def main() -> None:
    parser = argparse.ArgumentParser(description="The Morning Times news service")
    parser.add_argument("--config", default=os.environ.get("CONFIG_PATH", "config.yaml"),
                        help="Path to the YAML config file (env: CONFIG_PATH)")
    args = parser.parse_args()

    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings = load_settings(args.config)
    uvicorn.run(create_app(settings), host=settings.server.host, port=settings.server.port,
                log_config=None, proxy_headers=True)


if __name__ == "__main__":
    main()
