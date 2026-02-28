"""MutBot entry point: python -m mutbot"""

import argparse
import logging


def main():
    import mutbot

    parser = argparse.ArgumentParser(description="MutBot Web UI")
    parser.add_argument("-V", "--version", action="version", version=f"mutbot {mutbot.__version__}")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8741, help="Bind port (default: 8741)")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging to console")
    args = parser.parse_args()

    # Console handler — controlled by --debug flag
    console_level = logging.DEBUG if args.debug else logging.INFO
    logging.basicConfig(
        level=console_level,
        format="%(levelname)-8s %(name)s: %(message)s",
    )
    # The LogStore handler (DEBUG, full capture) is set up in server.py lifespan

    import uvicorn

    config = uvicorn.Config(
        "mutbot.web.server:app", host=args.host, port=args.port, log_level="info",
    )
    server = uvicorn.Server(config)

    # Override startup to print banner after uvicorn's own startup message
    _original_startup = server.startup

    async def _startup_with_banner(sockets=None):
        await _original_startup(sockets=sockets)
        print(f"\n  Open https://mutbot.ai to get started\n")

    server.startup = _startup_with_banner

    try:
        server.run()
    except KeyboardInterrupt:
        pass


main()
