"""MutBot entry point: python -m mutbot"""

import argparse
import logging


def main():
    parser = argparse.ArgumentParser(description="MutBot Web UI")
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

    # 首次启动向导：无模型配置时引导用户完成配置
    from mutbot.runtime.config import load_mutbot_config
    config = load_mutbot_config()
    if not config.get("providers"):
        from mutbot.cli.setup import run_setup_wizard
        run_setup_wizard()

    import uvicorn
    uvicorn.run("mutbot.web.server:app", host=args.host, port=args.port, log_level="info")


main()
