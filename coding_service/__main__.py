"""python -m coding_service --config service.json [--port 8790] [--ready-file path]

Starts the durable service. On startup it recovers tasks left over from the previous run (see DurableManager.recover) and prints
a one-line summary. Environment variables named in the configuration supply device tokens and the model key.
"""
import argparse
import json
import os
import sys
import threading
from pathlib import Path

import uvicorn

from .app import build_manager, create_app
from .config import ConfigError, load_config

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", required=True)
    parser.add_argument("--port", type=int, default=8790)
    parser.add_argument("--ready-file")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print("service refuses to start; the configuration has problems:", file=sys.stderr)
        for problem in exc.problems:
            print("  - " + problem, file=sys.stderr)
        return 2
    try:
        manager = build_manager(config, os.environ)
    except OSError as exc:
        print(f"service refuses to start: cannot open the task store ({type(exc).__name__}); it may be corrupt, locked or unwritable", file=sys.stderr)
        return 4
    try:
        summary = manager.recover()
    except OSError as exc:
        manager.shutdown()
        manager.store.close()
        print(f"service refuses to start: task history cannot be read ({type(exc).__name__}); inspect the store before retrying", file=sys.stderr)
        return 4
    print("recovery: " + ", ".join(f"{k}={len(v)}" for k, v in summary.items()), flush=True)
    server = uvicorn.Server(uvicorn.Config(create_app(config, manager), host="127.0.0.1", port=args.port, log_level="warning", access_log=False))
    if args.ready_file:
        def announce():
            while not server.started and not server.should_exit:
                threading.Event().wait(0.02)
            if server.started:
                Path(args.ready_file).write_text(json.dumps({"port": server.servers[0].sockets[0].getsockname()[1], "pid": os.getpid()}), encoding="utf-8")
        threading.Thread(target=announce, daemon=True).start()
    server.run()
    manager.shutdown()
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
