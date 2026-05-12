from __future__ import annotations
from http.server import BaseHTTPRequestHandler
from typing import Any, Optional, TYPE_CHECKING
import json

if TYPE_CHECKING:
    # Type-only import to avoid a runtime circular dependency: Agent owns
    # the server that uses Handler, so Handler can't import Agent at module
    # load time. TYPE_CHECKING is False at runtime, so this import never
    # actually runs — but type checkers see it.
    from opentrons_drivers.agent.base_agent import Agent


class Handler(BaseHTTPRequestHandler):
    """
    Per-request HTTP handler for the Opentrons agent.

    Runs on a fresh thread per incoming connection (spawned by
    ThreadingHTTPServer). Its only job is to translate HTTP requests into
    method calls on the Agent's slot — submit a job, read job status, etc.
    It NEVER touches hardware directly; that's the protocol thread's job.

    The Agent instance is bound via the class attribute `agent` before the
    server starts. BaseHTTPRequestHandler instantiates this class fresh on
    every request, so we can't pass `agent` through __init__ — but class
    attributes persist across instances, so every handler can reach the
    agent through `self.agent`.
    """

    # Bound by Agent.__init__ before the server starts accepting connections.
    agent: "Agent" = None  # type: ignore[assignment]

    # Suppress BaseHTTPRequestHandler's default stderr access logging,
    # which is noisy and not useful for our purposes.
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return

    # ------------------------------------------------------------------
    # I/O helpers
    # ------------------------------------------------------------------

    def _send_json(self, code: int, body: dict) -> None:
        payload = json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            # Client disconnected before we could reply. The action (if any)
            # is unaffected — it runs on the protocol thread and doesn't
            # depend on this connection. Agent state is also consistent
            # because the slot was already updated before this point.
            pass

    # Cap request bodies at 1 MiB. Action payloads are dicts of scalars
    # and small arrays; nothing legitimate gets near this. A misbehaving
    # client claiming Content-Length: 99999999999 would otherwise tie up
    # a handler thread waiting on bytes that never arrive.
    _MAX_BODY_BYTES = 1024 * 1024

    def _read_json_body(self) -> Optional[dict]:
        raw_len = self.headers.get("Content-Length", "0")
        try:
            n = int(raw_len)
        except ValueError:
            return None
        if n <= 0 or n > self._MAX_BODY_BYTES:
            return None
        try:
            raw = self.rfile.read(n)
            return json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None

    # ------------------------------------------------------------------
    # Routes
    # ------------------------------------------------------------------

    def do_GET(self) -> None:
        path = self.path.rstrip("/")

        if path == "/health" or path == "":
            ready = self.agent.is_ready()
            self._send_json(
                200 if ready else 503,
                {"ready": ready, "status": "ready" if ready else "initializing"},
            )
            return

        if path == "/actions/current":
            self._send_json(200, self.agent.current_job_view())
            return

        if path.startswith("/actions/"):
            job_id = path[len("/actions/"):]
            view = self.agent.job_view(job_id)
            if view is None:
                self._send_json(
                    404,
                    {"error": f"unknown or expired job_id '{job_id}'"},
                )
                return
            self._send_json(200, view)
            return

        self._send_json(404, {"error": f"no such route: {self.path}"})

    def do_POST(self) -> None:
        if self.path.rstrip("/") != "/actions":
            self._send_json(404, {"error": f"no such route: {self.path}"})
            return

        if not self.agent.is_ready():
            self._send_json(503, {"error": "agent not ready"})
            return

        body = self._read_json_body()
        if body is None or "action" not in body:
            self._send_json(
                400,
                {"error": "request body must be JSON with 'action' (str) and 'payload' (dict)"},
            )
            return

        action = body.get("action")
        payload = body.get("payload", {})

        if not isinstance(action, str) or not isinstance(payload, dict):
            self._send_json(
                400,
                {"error": "'action' must be str and 'payload' must be dict"},
            )
            return

        # All this method does, ultimately, is write the request into the
        # agent's slot under a lock. The actual hardware work happens later
        # on the protocol thread. This handler never blocks on hardware —
        # it returns 202 within milliseconds and the orchestrator polls
        # /actions/<job_id> for completion.
        accepted, info = self.agent.submit(action, payload)
        if not accepted:
            self._send_json(409, info)
            return
        self._send_json(202, info)