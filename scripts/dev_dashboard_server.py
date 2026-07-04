#!/usr/bin/env python3
"""Dev-only: run mcp/menubar/dashboard_server.py on a fixed port for local
preview/testing of the browser dashboard (the real one uses an ephemeral port
inside the menu bar process). Not part of the pipeline."""
import os
import sys
from http.server import ThreadingHTTPServer

_SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
# Test the SOURCE tree even when the shell inherits the installed package's env.
os.environ["S4L_REPO_DIR"] = _SRC
os.environ["SAPS_REPO_DIR"] = _SRC
sys.path.insert(0, os.path.join(_SRC, "mcp", "menubar"))
import dashboard_server as ds  # noqa: E402

srv = ThreadingHTTPServer(("127.0.0.1", 8765), ds._Handler)
print("http://127.0.0.1:8765/", flush=True)
srv.serve_forever()
