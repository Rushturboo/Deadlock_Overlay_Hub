"""Fake bridge that stages kills and team fights, for kill feed checks.

    py -3 _kill_bridge.py 8799
    py -3 hub.py --port 8790 --source mem --bridge http://127.0.0.1:8799/

steps through three shapes so one screenshot shows all of them: a clean
attribution, a team fight where one player takes most of it, and a team fight
with no clear leader (deaths only).
"""
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import _test_mem as t

DOC = t.fixture()
LOCK = threading.Lock()

# (killer index, kills) ... and victim indexes that died in the same window
STEPS = [
    ([(2, 1)], [7]),
    ([(2, 2), (4, 1)], [7, 9, 10]),
    ([(4, 1), (2, 1)], [7, 9]),
]


def stage_kills():
    i = 0
    while True:
        time.sleep(3.5)
        killers, victims = STEPS[i % len(STEPS)]
        i += 1
        with LOCK:
            players = DOC["snapshot"]["players"]
            for idx, n in killers:
                players[idx]["kills"] += n
            for idx in victims:
                players[idx]["deaths"] += 1
            # keep the boss losing health so the MID chip has something to show
            mid = DOC["snapshot"].get("timers", {}).get("midboss_state") or {}
            if isinstance(mid, dict) and mid.get("health"):
                mid["health"] = max(0, mid["health"] - 420)


class Bridge(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        with LOCK:
            body = json.dumps(DOC).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8799
    threading.Thread(target=stage_kills, daemon=True).start()
    print("kill bridge on http://127.0.0.1:%d/" % port, flush=True)
    ThreadingHTTPServer(("127.0.0.1", port), Bridge).serve_forever()
