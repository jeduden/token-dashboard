"""HTTP server: static frontend + JSON endpoints + SSE diff stream."""
from __future__ import annotations

import http.server
import json
import mimetypes
import queue
import sqlite3
import threading
import time
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from .db import (
    overview_totals, expensive_prompts, project_summary,
    tool_token_breakdown, recent_sessions, session_turns,
    daily_token_breakdown, model_breakdown, skill_breakdown,
)
from .pricing import cost_for, get_plan, set_plan
from .rates import load_with_override
from .pricing_targets import pricing_targets
from .whatif import parse_targets
from .whatif_report import whatif_report
from .repos import repo_summary, annotate_projects
from .tips import all_tips, dismiss_tip
from .scanner import scan_dir
from .scan_state import get_status, run_scan
from .skills import cached_catalog
from .cost_insights import tool_costs, skill_costs, top_calls
from .cache_insights import cache_report
from .compaction_insights import compactions_report
from .cost_drilldown import tool_params
from .skill_drilldown import skill_detail
from .call_detail import call_detail, NotFound
from .recent_feed import recent_feed
from .recent_buckets import message_buckets
from .session_timeline import session_timeline
from .session_costs import add_turn_costs, add_session_costs
from .session_subagents import session_subagents


WEB_ROOT = Path(__file__).resolve().parent.parent / "web"
PRICING_JSON = Path(__file__).resolve().parent.parent / "pricing.json"

EVENTS: "queue.Queue[dict]" = queue.Queue()

MAX_POST_BYTES = 1_000_000  # 1 MB — we only accept tiny JSON bodies (plan, tip key)
MAX_LIMIT = 1000


def _send_json(handler, obj, status: int = 200) -> None:
    body = json.dumps(obj, default=str).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


def _send_error(handler, status: int, msg: str) -> None:
    _send_json(handler, {"error": msg}, status=status)


def _emit(evt: dict) -> None:
    # Bound the queue: with no SSE client attached nothing drains it.
    while EVENTS.qsize() > 200:
        try:
            EVENTS.get_nowait()
        except queue.Empty:
            break
    EVENTS.put(evt)


def _db_busy(handler, e) -> None:
    try:
        _send_error(handler, 503, f"database busy: {e}")
    except (BrokenPipeError, ConnectionResetError):
        pass


def _clamp_limit(raw, default: int) -> int:
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return default
    return max(1, min(v, MAX_LIMIT))


def _serve_static(handler, rel: str) -> None:
    rel = rel.lstrip("/")
    p = (WEB_ROOT / rel).resolve()
    if not str(p).startswith(str(WEB_ROOT.resolve())) or not p.is_file():
        handler.send_response(404)
        handler.end_headers()
        return
    body = p.read_bytes()
    ctype, _ = mimetypes.guess_type(str(p))
    handler.send_response(200)
    handler.send_header("Content-Type", ctype or "application/octet-stream")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-cache")  # revalidate: the UI is edited in place
    handler.end_headers()
    handler.wfile.write(body)


def build_handler(db_path: str, projects_dir: str):
    pricing = load_with_override(PRICING_JSON)

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def do_HEAD(self):
            return self.do_GET()

        def do_GET(self):
            try:
                return self._do_get()
            except sqlite3.OperationalError as e:
                return _db_busy(self, e)
            except (BrokenPipeError, ConnectionResetError):
                pass  # the browser navigated away before we finished answering

        def _do_get(self):
            url = urlparse(self.path)
            qs = parse_qs(url.query or "")
            path = url.path
            since = qs.get("since", [None])[0]
            until = qs.get("until", [None])[0]
            try:
                targets = parse_targets(pricing, qs.get("compare", [""])[0])
            except ValueError as e:
                return _send_json(self, {"error": str(e)}, 400)
            if path in ("/", "/index.html"):
                return _serve_static(self, "index.html")
            if path.startswith("/web/"):
                return _serve_static(self, path[5:])
            if path == "/api/overview":
                totals = overview_totals(db_path, since, until)
                rep = whatif_report(db_path, pricing, "total", targets, since, until)["total"]
                totals["cost_usd"] = round(rep["cost_usd"], 4)
                if targets:
                    totals["alt_costs"] = rep["alt_costs"]
                    totals["alt_unpriced_tokens"] = rep["alt_unpriced_tokens"]
                return _send_json(self, totals)
            if path == "/api/prompts":
                limit = _clamp_limit(qs.get("limit", ["50"])[0], 50)
                sort = qs.get("sort", ["tokens"])[0]
                rows = expensive_prompts(db_path, limit=limit, sort=sort)
                for r in rows:
                    c = cost_for(r["model"], {
                        "input_tokens": 0, "output_tokens": 0,
                        "cache_read_tokens": r["cache_read_tokens"],
                        "cache_create_5m_tokens": 0, "cache_create_1h_tokens": 0,
                    }, pricing)
                    r["estimated_cost_usd"] = c["usd"]
                return _send_json(self, rows)
            if path == "/api/repos":
                return _send_json(self, repo_summary(db_path, since, until))
            if path == "/api/projects":
                return _send_json(self, annotate_projects(db_path, project_summary(db_path, since, until)))
            if path == "/api/tools":
                return _send_json(self, tool_token_breakdown(db_path, since, until))
            if path == "/api/sessions":
                return _send_json(self, add_session_costs(db_path, pricing, recent_sessions(
                    db_path, limit=_clamp_limit(qs.get("limit", ["20"])[0], 20),
                    since=since, until=until,
                ), targets))
            if path == "/api/session-timeline":
                sid = qs.get("id", [""])[0]
                if not sid:
                    return _send_json(self, {"error": "id is required"}, 400)
                return _send_json(self, session_timeline(db_path, pricing, sid, targets))
            if path == "/api/pricing/targets":
                return _send_json(self, pricing_targets(pricing))
            if path == "/api/whatif":
                try:
                    return _send_json(self, whatif_report(
                        db_path, pricing, qs.get("group", ["total"])[0] or "total",
                        qs.get("compare", [""])[0], since, until,
                        _clamp_limit(qs.get("limit", ["200"])[0], 200)))
                except ValueError as e:
                    return _send_json(self, {"error": str(e)}, 400)
            if path == "/api/session-subagents":
                sid = qs.get("id", [""])[0]
                if not sid:
                    return _send_json(self, {"error": "id is required"}, 400)
                return _send_json(self, session_subagents(db_path, pricing, sid, targets))
            if path == "/api/recent/buckets":
                sids = [x for x in qs.get("sessions", [""])[0].split(",") if x]
                try:
                    return _send_json(self, message_buckets(
                        db_path, pricing, qs.get("bucket", ["day"])[0] or "day",
                        since, until, sids or None, targets))
                except ValueError as e:
                    return _send_json(self, {"error": str(e)}, 400)
            if path == "/api/recent":
                sids = [x for x in qs.get("sessions", [""])[0].split(",") if x]
                return _send_json(self, recent_feed(
                    db_path, pricing, limit=_clamp_limit(qs.get("limit", ["100"])[0], 100),
                    since=since, until=until, sessions=sids or None, targets=targets))
            if path == "/api/daily":
                return _send_json(self, daily_token_breakdown(db_path, since, until))
            if path == "/api/skills":
                rows = skill_breakdown(db_path, since, until)
                catalog = cached_catalog()
                for r in rows:
                    info = catalog.get(r["skill"])
                    r["tokens_per_call"] = info["tokens"] if info else None
                return _send_json(self, rows)
            if path == "/api/cache":
                return _send_json(self, cache_report(db_path, pricing, since, until))
            if path == "/api/compactions":
                return _send_json(self, compactions_report(db_path, pricing, since, until))
            if path == "/api/by-model":
                rows = model_breakdown(db_path, since, until)
                priced = {x["key"]: x for x in whatif_report(
                    db_path, pricing, "model", targets, since, until, limit=1000)["rows"]}
                for r in rows:
                    x = priced.get(r["model"])
                    r["cost_usd"] = x["cost_usd"] if x else None
                    r["cost_estimated"] = x["estimated"] if x else True
                    if targets:
                        r["alt_costs"] = x["alt_costs"] if x else dict.fromkeys(targets)
                        r["alt_unpriced_tokens"] = x["alt_unpriced_tokens"] if x else dict.fromkeys(targets, 0)
                return _send_json(self, rows)
            if path.startswith("/api/sessions/"):
                sid = path.rsplit("/", 1)[1]
                return _send_json(self, add_turn_costs(session_turns(db_path, sid), pricing, targets))
            if path == "/api/tips":
                return _send_json(self, all_tips(db_path))
            if path == "/api/skill-costs/detail":
                limit = _clamp_limit(qs.get("limit", ["50"])[0], 50)
                try:
                    return _send_json(self, skill_detail(
                        db_path, pricing, qs.get("skill", [""])[0], since, until, limit,
                        qs.get("group", ["project"])[0] or "project"))
                except ValueError as e:
                    return _send_json(self, {"error": str(e)}, 400)
            if path == "/api/tool-costs/params":
                limit = _clamp_limit(qs.get("limit", ["50"])[0], 50)
                try:
                    return _send_json(self, tool_params(
                        db_path, pricing, qs.get("tool", [""])[0], since, until, limit,
                        qs.get("group", ["auto"])[0] or "auto"))
                except ValueError as e:
                    return _send_json(self, {"error": str(e)}, 400)
            if path == "/api/tool-costs/call":
                try:
                    cid = int(qs.get("id", [""])[0])
                except ValueError:
                    return _send_json(self, {"error": "id must be an integer tool_calls.id"}, 400)
                try:
                    return _send_json(self, call_detail(db_path, pricing, cid))
                except NotFound as e:
                    return _send_json(self, {"error": str(e)}, 404)
            if path in ("/api/tool-costs", "/api/skill-costs", "/api/tool-costs/calls"):
                limit = _clamp_limit(qs.get("limit", ["50"])[0], 50)
                if path == "/api/tool-costs":
                    return _send_json(self, tool_costs(db_path, pricing, since, until, limit))
                if path == "/api/skill-costs":
                    return _send_json(self, skill_costs(db_path, pricing, since, until, limit))
                tool = qs.get("tool", [None])[0] or None
                return _send_json(self, top_calls(db_path, pricing, since, until, tool, limit))
            if path == "/api/plan":
                return _send_json(self, {"plan": get_plan(db_path), "pricing": pricing})
            if path == "/api/scan/status":
                return _send_json(self, get_status())
            if path == "/api/scan":
                started, n = run_scan(projects_dir, db_path, scan_dir, _emit)
                if not started:
                    return _send_json(self, {"started": False, **get_status()})
                return _send_json(self, n)
            if path == "/api/stream":
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "keep-alive")
                self.end_headers()
                while True:
                    try:
                        evt = EVENTS.get(timeout=15)
                        chunk = f"data: {json.dumps(evt, default=str)}\n\n".encode()
                    except queue.Empty:
                        chunk = b": ping\n\n"
                    try:
                        self.wfile.write(chunk)
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError):
                        return
            self.send_response(404)
            self.end_headers()

        def do_POST(self):
            try:
                return self._do_post()
            except sqlite3.OperationalError as e:
                return _db_busy(self, e)

        def _do_post(self):
            url = urlparse(self.path)
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return _send_error(self, 400, "invalid Content-Length")
            if length < 0 or length > MAX_POST_BYTES:
                return _send_error(self, 413, f"body too large (max {MAX_POST_BYTES} bytes)")
            try:
                body = json.loads(self.rfile.read(length) or b"{}") if length else {}
            except json.JSONDecodeError:
                return _send_error(self, 400, "invalid JSON")
            if not isinstance(body, dict):
                return _send_error(self, 400, "body must be a JSON object")
            if url.path == "/api/plan":
                set_plan(db_path, body.get("plan", "api"))
                return _send_json(self, {"ok": True})
            if url.path == "/api/tips/dismiss":
                dismiss_tip(db_path, body.get("key", ""))
                return _send_json(self, {"ok": True})
            self.send_response(404)
            self.end_headers()

    return H


def _scan_loop(db_path: str, projects_dir: str, interval: float = 30.0):
    while True:
        try:
            started, n = run_scan(projects_dir, db_path, scan_dir, _emit)
            if started and n["messages"] > 0:
                EVENTS.put({"type": "scan", "n": n, "ts": time.time()})
        except Exception as e:
            EVENTS.put({"type": "error", "message": str(e)})
        time.sleep(interval)


def run(host: str, port: int, db_path: str, projects_dir: str, scan: bool = True):
    """Start listening immediately; the first scan runs in the background."""
    if scan:
        threading.Thread(target=_scan_loop, args=(db_path, projects_dir), daemon=True).start()
    H = build_handler(db_path, projects_dir)
    httpd = http.server.ThreadingHTTPServer((host, port), H)
    httpd.serve_forever()
