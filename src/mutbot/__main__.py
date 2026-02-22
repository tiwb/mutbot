"""MutBot entry point: python -m mutbot"""

import argparse


def main():
    parser = argparse.ArgumentParser(description="MutBot Web UI")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8741, help="Bind port (default: 8741)")
    args = parser.parse_args()

    import uvicorn
    uvicorn.run("mutbot.web.server:app", host=args.host, port=args.port)


main()
