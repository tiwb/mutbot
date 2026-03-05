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

    # Console handler — priority: --debug > config > default WARNING
    if args.debug:
        console_level = logging.DEBUG
    else:
        from mutbot.runtime.config import load_mutbot_config
        cfg = load_mutbot_config()
        level_name = cfg.get("logging.console_level", default="WARNING")
        console_level = getattr(logging, level_name.upper(), logging.WARNING)
    logging.basicConfig(
        level=console_level,
        format="%(levelname)-8s %(name)s: %(message)s",
    )
    # The LogStore handler (DEBUG, full capture) is set up in server.py lifespan

    import uvicorn

    uvi_level = "debug" if console_level <= logging.DEBUG else "info" if console_level <= logging.INFO else "warning"
    config = uvicorn.Config(
        "mutbot.web.server:app", host=args.host, port=args.port, log_level=uvi_level,
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
