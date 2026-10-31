"""python -m fleet_agent [--port 8780] [--captain http://drone:8766] [--no-browser]"""
from __future__ import annotations

import argparse
import threading
import webbrowser

import uvicorn

from .config import Config
from .server import create_app


def main() -> None:
    ap = argparse.ArgumentParser(prog="fleet_agent")
    ap.add_argument("--port", type=int)
    ap.add_argument("--captain", help="Captain web server address, e.g. http://192.168.1.20:8766")
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()
    cfg = Config.build(args.port, args.captain, not args.no_browser)
    app, agent = create_app(cfg)
    url = agent.guard.launch_url()
    print(f"\nFleet Agent is running. Open this link (it carries this run's access token):\n\n    {url}\n")
    if cfg.open_browser:
        threading.Timer(1.0, webbrowser.open, args=(url,)).start()
    uvicorn.run(app, host="127.0.0.1", port=cfg.port, log_level="warning")


if __name__ == "__main__":
    main()
