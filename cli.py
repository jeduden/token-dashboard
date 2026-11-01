"""Token Dashboard CLI entrypoint."""
from __future__ import annotations

import argparse
import os
import webbrowser
from datetime import datetime, timedelta, timezone
from pathlib import Path

from token_dashboard.db import init_db, default_db_path, overview_totals
from token_dashboard.scanner import scan_dir
from token_dashboard.tips import all_tips


def _db_path(args) -> str:
    return args.db or os.environ.get("TOKEN_DASHBOARD_DB") or str(default_db_path())


def _projects(args) -> str:
    return (
        args.projects_dir
        or os.environ.get("CLAUDE_PROJECTS_DIR")
        or str(Path.home() / ".claude" / "projects")
    )


def _today_range():
    now = datetime.now(timezone.utc)
    start = datetime(now.year, now.month, now.day, tzinfo=timezone.utc).isoformat()
    end = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    return start, end


def cmd_scan(args):
    db = _db_path(args)
    init_db(db)
    n = scan_dir(_projects(args), db)
    print(f"Token Dashboard: scanned {n['files']} files, {n['messages']} messages, {n['tools']} tool calls")


def cmd_today(args):
    db = _db_path(args)
    init_db(db)
    s, e = _today_range()
    t = overview_totals(db, since=s, until=e)
    print("Token Dashboard — today")
    print(f"  sessions: {t['sessions']}    turns: {t['turns']}")
    print(f"  input:    {t['input_tokens']:>12,}    output: {t['output_tokens']:>12,}")
    print(f"  cache rd: {t['cache_read_tokens']:>12,}    cache cr: {t['cache_create_5m_tokens']+t['cache_create_1h_tokens']:>12,}")


def cmd_stats(args):
    db = _db_path(args)
    init_db(db)
    t = overview_totals(db)
    print("Token Dashboard — all time")
    print(f"  sessions: {t['sessions']}    turns: {t['turns']}")
    print(f"  input:    {t['input_tokens']:>12,}    output: {t['output_tokens']:>12,}")


def cmd_tips(args):
    db = _db_path(args)
    init_db(db)
    tips = all_tips(db)
    if not tips:
        print("Token Dashboard: no suggestions")
        return
    for tip in tips:
        print(f"[{tip['category']}] {tip['title']}")
        print(f"  {tip['body']}\n")


def cmd_findings_data(args):
    from token_dashboard.findings_facts import dumps
    from token_dashboard.rates import load_with_override
    db = _db_path(args)
    init_db(db)
    print(dumps(db, load_with_override(Path(__file__).resolve().parent / "pricing.json"), args.days))


def cmd_findings_save(args):
    import json
    import sys
    from token_dashboard import findings_store as fs
    db = _db_path(args)
    init_db(db)
    try:
        doc = json.load(sys.stdin if args.file == "-" else open(args.file, encoding="utf-8"))
        saved = fs.save(db, doc)
    except (OSError, ValueError) as e:      # fs.Invalid is a ValueError, as is bad JSON
        raise SystemExit(f"Token Dashboard: findings not saved: {e}")
    print(f"Token Dashboard: saved {len(saved['findings'])} findings and {len(saved['recommendations'])} "
          f"recommendations for the last {saved['window_days']} days; see the Tips tab")


def cmd_findings_clear(args):
    from token_dashboard import findings_store as fs
    db = _db_path(args)
    init_db(db)
    fs.clear(db)
    print("Token Dashboard: cleared the saved findings")


def cmd_dashboard(args):
    db = _db_path(args)
    init_db(db)
    from token_dashboard.server import run

    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8080"))
    url = f"http://{host}:{port}/"
    if not args.no_open:
        webbrowser.open(url)
    print(f"Token Dashboard listening on {url}")
    run(host, port, db, _projects(args), scan=not args.no_scan)


def _common_flags(suppress: bool) -> argparse.ArgumentParser:
    """Shared --db/--projects-dir flags.

    The subcommands re-declare these so they also work after the subcommand.
    Their copies must default to SUPPRESS: otherwise an unset subcommand flag
    overwrites a value given before the subcommand with None, silently
    falling back to the default (live) database.
    """
    kw = {"default": argparse.SUPPRESS} if suppress else {}
    c = argparse.ArgumentParser(add_help=False)
    c.add_argument("--db", help="SQLite path (default ~/.claude/token-dashboard.db)", **kw)
    c.add_argument("--projects-dir", help="JSONL root (default ~/.claude/projects)", **kw)
    return c


def build_parser() -> argparse.ArgumentParser:
    top, sub_flags = _common_flags(False), _common_flags(True)
    p = argparse.ArgumentParser(prog="token-dashboard", description="Local Claude Code usage dashboard", parents=[top])
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("scan",  parents=[sub_flags]).set_defaults(func=cmd_scan)
    sub.add_parser("today", parents=[sub_flags]).set_defaults(func=cmd_today)
    sub.add_parser("stats", parents=[sub_flags]).set_defaults(func=cmd_stats)
    sub.add_parser("tips",  parents=[sub_flags]).set_defaults(func=cmd_tips)
    fd = sub.add_parser("findings-data", parents=[sub_flags], help="print the facts Claude reads to write findings")
    fd.add_argument("--days", type=int, default=7)
    fd.set_defaults(func=cmd_findings_data)
    fsv = sub.add_parser("findings-save", parents=[sub_flags], help="store findings written as JSON (file or - for stdin)")
    fsv.add_argument("file")
    fsv.set_defaults(func=cmd_findings_save)
    sub.add_parser("findings-clear", parents=[sub_flags]).set_defaults(func=cmd_findings_clear)
    d = sub.add_parser("dashboard", parents=[sub_flags])
    d.add_argument("--no-scan", action="store_true")
    d.add_argument("--no-open", action="store_true")
    d.set_defaults(func=cmd_dashboard)
    return p


def main():
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
