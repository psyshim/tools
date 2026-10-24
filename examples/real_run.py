"""Real-model smoke run (host-side sample; not part of the package).

Env: OPENAI_API_KEY, MODEL (required); OPENAI_BASE_URL (default https://api.openai.com/v1, any OpenAI-compatible endpoint).
Usage: python examples/real_run.py PROJECT_DIR "instruction" [test-argv...]
Every apply / test run asks y/n on the terminal.
"""
import json
import os
import sys
import time
import urllib.request
from code_attach.engine import Engine
from code_attach.providers import from_openai_response, to_openai
from coding_agent.activity_log import ActivityLog, default_log_dir

def complete(messages, tools):
    payload = {"model": os.environ["MODEL"], **to_openai(messages, tools)}
    req = urllib.request.Request(os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1") + "/chat/completions",
                                 json.dumps(payload).encode(), {"Content-Type": "application/json", "Authorization": "Bearer " + os.environ["OPENAI_API_KEY"]})
    with urllib.request.urlopen(req, timeout=120) as r:
        return from_openai_response(r.read(4 * 1024 * 1024 + 1))

def main(project, instruction, test_argv):
    activity = ActivityLog(default_log_dir(project))
    print("Task logs:", activity.directory)
    engine = Engine(complete, {"tests": test_argv} if test_argv else None, event_sink=activity.record)
    tid, _ = engine.start("local", project, "run-1", instruction)
    seen = 0
    while True:
        r = engine.events("local", tid, seen)
        for e in r["events"]:
            seen += 1
            print(e["type"], json.dumps(e["data"], ensure_ascii=False)[:1500])
            if e["type"] == "approval_required":
                ok = input("approve? [y/N] ").strip().lower() == "y"
                engine.decide("local", tid, e["data"]["action_id"], e["data"]["fingerprint"], ok)
        if r["terminal"] and not r["events"]:
            return r["state"]
        time.sleep(0.3)

if __name__ == "__main__":
    print("final:", main(sys.argv[1], sys.argv[2], sys.argv[3:]))
