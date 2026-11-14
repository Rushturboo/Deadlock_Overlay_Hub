"""Serve the _test_mem fixture as a fake bridge, so widgets can be checked without the game."""
import sys
from http.server import ThreadingHTTPServer

import _test_mem as t

if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8799
    print("fake bridge on http://127.0.0.1:%d/" % port)
    ThreadingHTTPServer(("127.0.0.1", port), t.Bridge).serve_forever()
