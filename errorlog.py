#!/usr/bin/env python3
"""
Bug and error log for the scheduler (standard library only).

Two files in logs/ (created on first use, rotated at 1 MB, 5 old copies kept):
  app.log       readable log of everything: starts, solves, saves, warnings, errors
  errors.jsonl  one JSON record per error, for looking back at what went wrong

Each record has a short id (shown to the user, e.g. "E-3f9a2c") so a message on
screen can be matched to its traceback here. Kinds:
  bug    unexpected exception in the server, the command line or the browser page
  input  invalid config or request (the user can fix it); repeats are not re-logged

  python3 errorlog.py              # last 20 errors
  python3 errorlog.py -n 50 --bugs # last 50, bugs only
  python3 errorlog.py --id E-3f9a2c
  python3 errorlog.py --clear
"""

import argparse
import datetime as dt
import json
import logging
import logging.handlers
import os
import sys
import threading
import traceback
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.environ.get("PLANNER_LOG_DIR", os.path.join(HERE, "logs"))
MAX_BYTES = 1_000_000
BACKUPS = 5
REPEAT_WINDOW = 300  # seconds: the same input error within this window is logged once

log = logging.getLogger("planner")
_lock = threading.Lock()
_last_input = {}  # (source, message) -> time last written
_ready = False


def setup(console=False):
    """Attach the rotating file handler (once). console=True also echoes warnings to stderr."""
    global _ready
    with _lock:
        if _ready:
            return log
        os.makedirs(LOG_DIR, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(os.path.join(LOG_DIR, "app.log"), maxBytes=MAX_BYTES,
                                                  backupCount=BACKUPS, encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s"))
        log.addHandler(fh)
        if console:
            ch = logging.StreamHandler(sys.stderr)
            ch.setLevel(logging.WARNING)
            ch.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
            log.addHandler(ch)
        log.setLevel(logging.INFO)
        log.propagate = False
        _ready = True
        return log


def _rotate(path):
    if os.path.exists(path) and os.path.getsize(path) >= MAX_BYTES:
        for i in range(BACKUPS - 1, 0, -1):
            if os.path.exists(f"{path}.{i}"):
                os.replace(f"{path}.{i}", f"{path}.{i + 1}")
        os.replace(path, f"{path}.1")


def record(kind, source, message, exc=None, tb=None, context=None):
    """Write one error record and return its id (None if it repeats a recent input error)."""
    setup()
    now = dt.datetime.now()
    if kind == "input":
        key = (source, message)
        with _lock:
            last = _last_input.get(key)
            _last_input[key] = now.timestamp()
        if last is not None and now.timestamp() - last < REPEAT_WINDOW:
            return None
    if exc is not None and tb is None:
        tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    err_id = "E-" + uuid.uuid4().hex[:6]
    rec = {"id": err_id, "time": now.isoformat(timespec="seconds"), "kind": kind, "source": source,
           "message": message}
    if exc is not None:
        rec["type"] = type(exc).__name__
    if tb:
        rec["traceback"] = tb
    if context:
        rec["context"] = context
    path = os.path.join(LOG_DIR, "errors.jsonl")
    try:
        with _lock:
            _rotate(path)
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
    except OSError as e:  # logging must never take the app down
        print(f"errorlog: could not write {path}: {e}", file=sys.stderr)
    level = logging.ERROR if kind == "bug" else logging.WARNING
    log.log(level, "[%s] %s %s: %s%s", err_id, kind, source, message, f"\n{tb.rstrip()}" if tb else "")
    return err_id


def bug(source, exc, context=None):
    return record("bug", source, f"{type(exc).__name__}: {exc}", exc=exc, context=context)


def read(limit=20, kind=None, err_id=None):
    """Most recent records first (current file and rotated copies)."""
    recs = []
    path = os.path.join(LOG_DIR, "errors.jsonl")
    for p in [path] + [f"{path}.{i}" for i in range(1, BACKUPS + 1)]:
        if not os.path.exists(p):
            continue
        with open(p, encoding="utf-8") as f:
            lines = f.readlines()
        for line in reversed(lines):
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if err_id and rec.get("id") != err_id:
                continue
            if kind and rec.get("kind") != kind:
                continue
            recs.append(rec)
            if len(recs) >= limit:
                return recs
    return recs


def clear():
    path = os.path.join(LOG_DIR, "errors.jsonl")
    for p in [path] + [f"{path}.{i}" for i in range(1, BACKUPS + 1)]:
        if os.path.exists(p):
            os.remove(p)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Show the scheduler's bug and error log")
    ap.add_argument("-n", type=int, default=20, help="how many records (default 20)")
    ap.add_argument("--bugs", action="store_true", help="only unexpected exceptions")
    ap.add_argument("--id", help="show one record by id, e.g. E-3f9a2c")
    ap.add_argument("--clear", action="store_true", help="delete errors.jsonl and its rotated copies")
    args = ap.parse_args(argv)
    if args.clear:
        clear()
        print("error log cleared")
        return 0
    recs = read(args.n, "bug" if args.bugs else None, args.id)
    if not recs:
        print("no errors logged" if not args.id else f"{args.id} not found")
        return 0 if not args.id else 1
    for rec in reversed(recs):
        print(f"{rec['time']}  {rec['id']}  {rec['kind']:<5}  {rec['source']}: {rec['message']}")
        if rec.get("context"):
            print(f"    context: {json.dumps(rec['context'], ensure_ascii=False)[:300]}")
        if rec.get("traceback") and (args.id or rec["kind"] == "bug"):
            print("    " + rec["traceback"].rstrip().replace("\n", "\n    "))
    return 0


if __name__ == "__main__":
    sys.exit(main())
