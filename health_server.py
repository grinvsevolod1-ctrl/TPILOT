# -*- coding: utf-8 -*-
"""Read-only HTTP health endpoint for TPilot (stdlib only)."""
from __future__ import annotations

import json
import logging
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, Tuple

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env.TPilot"
LOG_PATH = BASE_DIR / "logs" / "health_server.log"

_HEALTH_SAFE_KEYS = (
    "ok",
    "time_utc",
    "name",
    "reasons",
    "active_managers",
    "main_processes",
    "manager_processes",
    "panel_processes",
    "duplicate_warnings",
)


def _load_env(path: Path) -> Dict[str, str]:
    out: Dict[str, str] = {}
    if not path.exists():
        return out
    for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def _apply_env_file() -> None:
    for k, v in _load_env(ENV_PATH).items():
        if k and os.environ.get(k) is None:
            os.environ[k] = v


_apply_env_file()

ENV = _load_env(ENV_PATH)
for _k, _v in ENV.items():
    if _k:
        os.environ.setdefault(_k, _v)

HEALTH_SERVER_BIND = (os.getenv("HEALTH_SERVER_BIND") or ENV.get("HEALTH_SERVER_BIND") or "100.106.38.73").strip()
try:
    HEALTH_SERVER_PORT = int(str(os.getenv("HEALTH_SERVER_PORT") or ENV.get("HEALTH_SERVER_PORT") or "8098").strip() or "8098")
except Exception:
    HEALTH_SERVER_PORT = 8098
HEALTH_SERVER_TOKEN = (os.getenv("HEALTH_SERVER_TOKEN") or ENV.get("HEALTH_SERVER_TOKEN") or "").strip()
STATUS_FILE = BASE_DIR / str(
    os.getenv("WATCHDOG_STATUS_FILE") or ENV.get("WATCHDOG_STATUS_FILE") or "runtime/soft_status.json"
).strip()


def _setup_logging() -> None:
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(LOG_PATH, encoding="utf-8"), logging.StreamHandler()],
    )


def _read_bearer_token(header_val: str) -> str:
    raw = str(header_val or "").strip()
    if raw.lower().startswith("bearer "):
        return raw[7:].strip()
    return ""


def _sanitize_status_payload(data: dict) -> dict:
    """Expose only soft-status fields; never panel/command execution data."""
    out: dict = {}
    for key in _HEALTH_SAFE_KEYS:
        if key in data:
            out[key] = data[key]
    if "ok" not in out:
        out["ok"] = bool(data.get("ok"))
    return out


def _load_health_payload() -> Tuple[int, dict]:
    try:
        if not STATUS_FILE.exists():
            return 200, {"ok": False, "reason": "soft_status.json missing"}
        raw = STATUS_FILE.read_text(encoding="utf-8", errors="ignore")
        data = json.loads(raw or "{}")
        if not isinstance(data, dict):
            return 200, {"ok": False, "reason": "soft_status.json invalid"}
        return 200, _sanitize_status_payload(data)
    except Exception as exc:
        return 200, {"ok": False, "reason": f"soft_status.json unreadable: {exc!r}"}


class HealthHandler(BaseHTTPRequestHandler):
    server_version = "TPilotHealth/1.0"

    def log_message(self, fmt: str, *args) -> None:
        logging.info("%s - %s", self.address_string(), fmt % args)

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        if not HEALTH_SERVER_TOKEN:
            return False
        token = _read_bearer_token(self.headers.get("Authorization", ""))
        return bool(token) and token == HEALTH_SERVER_TOKEN

    def do_GET(self) -> None:
        path = (self.path or "").split("?", 1)[0]
        if path != "/health":
            self._send_json(404, {"ok": False, "reason": "not found"})
            return
        if not self._authorized():
            self._send_json(401, {"ok": False, "reason": "unauthorized"})
            return
        code, payload = _load_health_payload()
        self._send_json(code, payload)

    def do_POST(self) -> None:
        self._send_json(405, {"ok": False, "reason": "method not allowed"})


def main() -> int:
    _setup_logging()
    if not HEALTH_SERVER_TOKEN:
        logging.error("HEALTH_SERVER_TOKEN is not set; refusing to start without auth token")
        return 2
    host = HEALTH_SERVER_BIND or "127.0.0.1"
    port = int(HEALTH_SERVER_PORT)
    httpd = ThreadingHTTPServer((host, port), HealthHandler)
    logging.info("health_server listening on %s:%s status_file=%s", host, port, STATUS_FILE)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        logging.info("health_server stopped")
        return 0
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
