"""Federation comms emitter — fire-and-forget POST to the dashboard mesh.

Pure observability for the agent-comms visualiser (dashboard on :3010,
/comms.html). NEVER blocks the parent program and NEVER raises into the caller:
the send runs on a daemon thread with a short timeout and every error is
swallowed. If the dashboard is down, the event is silently dropped — the
automation path is completely unaffected.

Usage:
    from . import mesh
    mesh.emit("connect-engine", "drip-engine", summary="accepted -> drip")
"""
import json
import threading
import urllib.request

_URL = "http://127.0.0.1:3010/api/emit"


def emit(from_agent, to_agent, summary="", type="handoff", source_app="engine"):
    """Fire a comms event at the mesh. Returns immediately; never raises."""
    def _send():
        try:
            data = json.dumps({
                "source_app": source_app, "from": from_agent, "to": to_agent,
                "type": type, "summary": str(summary)[:160],
            }).encode()
            req = urllib.request.Request(
                _URL, data=data, headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=1.5).read()
        except Exception:
            pass  # dashboard down / any error → drop silently
    try:
        threading.Thread(target=_send, daemon=True).start()
    except Exception:
        pass
