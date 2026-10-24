"""python -m device_agent --config grants.json [--port 0] [--ready-file path]

Binds 127.0.0.1 only. With --port 0 the OS picks a free port; --ready-file receives {"port": N, "pid": P} once serving, so
a supervisor (or a test) learns where the device is without guessing. The bearer token is NOT in the grants file (only its hash):
the operator hands the token to the coding service's configuration out of band.
"""
import argparse
import json
import sys
import threading
from pathlib import Path

import uvicorn

from .config import ConfigError, load_config
from .server import create_app

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", required=True)
    parser.add_argument("--port", type=int, default=8780)
    parser.add_argument("--ready-file")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print("device refuses to start; the grants file has problems:", file=sys.stderr)
        for problem in exc.problems:
            print("  - " + problem, file=sys.stderr)
        return 2
    from coding_agent.execution_owner import OwnerBusy
    try:
        app = create_app(config)
    except OwnerBusy:
        print("device refuses to start: another device agent already owns one of these workspaces", file=sys.stderr)
        return 3
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=args.port, log_level="warning", access_log=False))
    if args.ready_file:
        def announce():
            while not server.started and not server.should_exit:
                threading.Event().wait(0.02)
            if server.started:
                port = server.servers[0].sockets[0].getsockname()[1]
                Path(args.ready_file).write_text(json.dumps({"port": port, "pid": __import__("os").getpid()}), encoding="utf-8")
        threading.Thread(target=announce, daemon=True).start()
    server.run()
    app.state.service.close()
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
