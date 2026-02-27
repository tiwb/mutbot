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

    # 始终启动 Web 服务器，无 LLM 配置时通过 Web 向导完成配置
    import uvicorn
    uvicorn.run("mutbot.web.server:app", host=args.host, port=args.port, log_level="info")


main()
