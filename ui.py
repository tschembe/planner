#!/usr/bin/env python3
"""
Browser UI for scheduler.py (standard library only).

  python3 ui.py                      # opens http://127.0.0.1:8765
  python3 ui.py --config my.json     # load/save this config file
  python3 ui.py --port 9000 --no-browser

The page edits employees, shifts, rules and a per-employee availability calendar,
runs the solver and shows the schedule. Every edit is autosaved to the config file, and the
last schedule of each month to schedule_YYYY-MM.json next to it;
availability is stored as compact rules (default, weekday+shift patterns, dated exceptions).
"""

import argparse
import calendar
import collections
import datetime as dt
import io
import json
import os
import re
import sys
import threading
import time
import webbrowser
from urllib.parse import parse_qs, urlsplit
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import errorlog
import i18n
import scheduler as sch
from i18n import _
from xlsx_export import build_schedule_xlsx

HERE = os.path.dirname(os.path.abspath(__file__))
SAVE_LOCK = threading.Lock()


def code_stamp():
    """Modification times of the Python files this server runs; if they change, it needs a restart."""
    return [os.path.getmtime(os.path.join(HERE, f)) for f in ("ui.py", "scheduler.py", "xlsx_export.py", "i18n.py", "errorlog.py")]


STARTED_WITH = code_stamp()


def shown(path):
    """Path relative to the project when it is inside it, else absolute."""
    rel = os.path.relpath(path, HERE)
    return path if rel.startswith("..") else rel


LEVEL_WORDS = ["must", "can", "neutral", "never"]
WEEKDAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
CODE_TO_LEVEL = {c: i for i, c in enumerate(sch.LEVEL_CODES)}


def resolve(cfg, month, base_dir=HERE):
    """Config (with availability rules) -> dates, shifts and a grid of level codes per employee."""
    # the night-shift checkbox is applied by the solver, not baked into the painted grid
    cfg = {**cfg, "employees": [{k: v for k, v in e.items() if k != "night_shifts"} if isinstance(e, dict) else e
                                for e in cfg.get("employees", [])]}
    csv_path = cfg.get("availability_csv")
    if csv_path and not os.path.isabs(csv_path):
        csv_path = os.path.join(base_dir, csv_path)  # relative to the config file, as on the command line
    p = sch.Problem(cfg, month, csv_path if csv_path and os.path.exists(csv_path) else None)
    grid = {p.names[e]: [[sch.LEVEL_CODES[lvl] for lvl in day] for day in p.pref[e]] for e in range(p.E)}
    return {"month": p.month, "dates": [d.isoformat() for d in p.dates], "grid": grid}


def compact(cells, dates, shift_names):
    """Grid of level codes for one employee -> availability rules understood by scheduler.Problem."""
    flat = [c for day in cells for c in day]
    default = collections.Counter(flat).most_common(1)[0][0] if flat else "N"
    rules = {"default": LEVEL_WORDS[CODE_TO_LEVEL[default]]}
    pattern = {}
    for wd in range(7):
        days = [d for d, date in enumerate(dates) if date.weekday() == wd]
        for s, name in enumerate(shift_names):
            if not days:
                continue
            mode = collections.Counter(cells[d][s] for d in days).most_common(1)[0][0]
            if mode != default:
                pattern[(wd, s)] = mode
    if pattern:
        rules["weekday_shifts"] = {f"{WEEKDAY_NAMES[wd]} {shift_names[s]}": LEVEL_WORDS[CODE_TO_LEVEL[c]]
                                   for (wd, s), c in sorted(pattern.items())}
    exceptions = {}
    for d, date in enumerate(dates):
        for s, name in enumerate(shift_names):
            base = pattern.get((date.weekday(), s), default)
            if cells[d][s] != base:
                exceptions[f"{date.isoformat()} {name}"] = LEVEL_WORDS[CODE_TO_LEVEL[cells[d][s]]]
    if exceptions:
        rules["date_shifts"] = exceptions
    return rules


def config_from_payload(payload):
    """UI payload {config, grid} -> full config with the grid folded into availability rules."""
    cfg = dict(payload["config"])
    cfg.pop("availability_csv", None)
    grid = payload.get("grid", {})
    y, m = (int(x) for x in cfg["month"].split("-"))
    dates = [dt.date(y, m, d) for d in range(1, calendar.monthrange(y, m)[1] + 1)]
    shift_names = [s["name"] for s in cfg.get("shifts", [])]
    avail = {}
    for emp in cfg.get("employees", []):
        name = emp["name"] if isinstance(emp, dict) else emp
        cells = grid.get(name)
        if not cells or len(cells) != len(dates) or any(len(day) != len(shift_names) for day in cells):
            raise sch.ConfigError(_("The availability of {name!r} does not match the month or the shifts. Reload the page.", name=name))
        for day in cells:
            for c in day:
                if c not in CODE_TO_LEVEL:
                    raise sch.ConfigError(f"availability for {name!r}: bad code {c!r}")
        avail[name] = compact(cells, dates, shift_names)
    cfg["availability"] = avail
    return cfg


def write_json(path, data):
    """Write JSON safely: one writer at a time, and via a temp file so a crash never leaves half a file."""
    with SAVE_LOCK:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp, path)


MONTH_RE = re.compile(r"\d{4}-(0[1-9]|1[0-2])")
SCHEDULE_FILE_RE = re.compile(r"schedule_(\d{4}-(0[1-9]|1[0-2]))\.json")


def saved_schedules(folder):
    """{month: schedule} for every schedule_YYYY-MM.json in the folder."""
    out = {}
    for name in sorted(os.listdir(folder)):
        m = SCHEDULE_FILE_RE.fullmatch(name)
        if m:
            try:
                with open(os.path.join(folder, name), encoding="utf-8") as f:
                    out[m.group(1)] = json.load(f)
            except (OSError, json.JSONDecodeError):
                errorlog.log.warning("backup: skipped unreadable %s", name)
    return out


def remove_schedules(folder):
    for name in os.listdir(folder):
        if SCHEDULE_FILE_RE.fullmatch(name):
            os.remove(os.path.join(folder, name))


def make_backup(cfg, folder):
    """Everything the app keeps (config with availability, all saved schedules) as one JSON document."""
    return {"shift_planner_backup": 1, "created": dt.datetime.now().isoformat(timespec="seconds"),
            "config": cfg, "schedules": saved_schedules(folder)}


def restore_backup(data, config_path):
    """Replace the config and all saved schedules with a backup file (or a plain config file)."""
    if not isinstance(data, dict):
        raise sch.ConfigError(_("This file is not a backup of the shift planner."))
    if "shift_planner_backup" in data:
        cfg, schedules = data.get("config"), data.get("schedules") or {}
    elif "employees" in data or "shifts" in data:
        cfg, schedules = data, {}  # a plain config.json
    else:
        raise sch.ConfigError(_("This file is not a backup of the shift planner."))
    if not isinstance(cfg, dict) or not MONTH_RE.fullmatch(str(cfg.get("month", ""))) or not isinstance(schedules, dict):
        raise sch.ConfigError(_("This file is not a backup of the shift planner."))
    if cfg.get("shifts"):
        sch.Problem(cfg)  # validate before anything is overwritten
    for month, result in schedules.items():
        if not MONTH_RE.fullmatch(month) or not isinstance(result, dict) or result.get("month") != month:
            raise sch.ConfigError(_("This file is not a backup of the shift planner."))
    folder = os.path.dirname(config_path)
    remove_schedules(folder)
    for month, result in schedules.items():
        write_json(os.path.join(folder, f"schedule_{month}.json"), result)
    write_json(config_path, cfg)


def delete_all_data(config_path, month):
    """Empty config (no employees, shifts or availability) and no saved schedules."""
    remove_schedules(os.path.dirname(config_path))
    # an empty config rather than no file, else the next start would load example_config.json
    write_json(config_path, {"month": month if MONTH_RE.fullmatch(str(month)) else dt.date.today().strftime("%Y-%m"),
                             "employees": [], "shifts": []})


def run_solver(cfg, iterations, restarts, seed):
    p = sch.Problem(cfg)
    log = io.StringIO()
    t0 = time.time()
    sv = sch.solve(p, iterations, restarts, seed, log=log)
    return result_dict(p, sv, time.time() - t0, log.getvalue().strip().splitlines())


def evaluate(cfg, slots_in):
    """Re-check a schedule the admin changed by hand: same result format as run_solver, no search."""
    p = sch.Problem(cfg)
    wanted = {(sl["d"], sl["s"]): [n for n in sl.get("people", []) if isinstance(n, str)] for sl in slots_in}
    assign = []
    for k, (d, s) in enumerate(p.slots):
        row = []
        for n in wanted.get((d, s), []):
            if n in p.names and p.names.index(n) not in row:
                row.append(p.names.index(n))
        row = row[:p.need[k]]
        assign.append(row + [-1] * (p.need[k] - len(row)))
    sv = sch.Solver(p)
    sv.load(assign)
    return result_dict(p, sv, 0, [])


def result_dict(p, sv, seconds, log):
    stats, problems = sch.summarize(p, sv)
    slots = []
    for k, (d, s) in enumerate(p.slots):
        slots.append({"d": d, "s": s, "need": p.need[k],
                      "people": [None if e == -1 else
                                 {"name": p.names[e], "level": sch.LEVEL_CODES[p.pref[e][d][s]]}
                                 for e in sv.assign[k]]})
    return {
        "month": p.month,
        "dates": [d.isoformat() for d in p.dates],
        "weekend": p.is_weekend,
        "shifts": [{"name": n, "label": l} for n, l in zip(p.shift_names, p.shift_label)],
        "employees": p.names,
        "slots": slots,
        "summary": stats,
        "problems": problems,
        "cost": sv.total(),
        "log": log,
        "seconds": round(seconds, 1),
    }


class Handler(BaseHTTPRequestHandler):
    config_path = None

    def schedule_path(self, month):
        if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", str(month)):
            raise sch.ConfigError(f"bad month {month!r}")
        return os.path.join(os.path.dirname(self.config_path), f"schedule_{month}.json")

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype="application/json", headers=None):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass  # the tab was closed or reloaded while a request (e.g. a long solve) was running

    def do_GET(self):
        i18n.set_lang(self.headers.get("X-Lang", "en"))  # language of messages for this request
        try:
            self._get()
        except Exception as exc:  # unexpected: log it with an id the user can quote
            self._unexpected(exc)

    def _unexpected(self, exc):
        err_id = errorlog.bug(f"server {self.command} {urlsplit(self.path).path}", exc)
        self._send(500, {"error": _("Unexpected error ({id}). Details: python3 errorlog.py --id {id}", id=err_id)})

    def _get(self):
        path = urlsplit(self.path).path
        if path in ("/", "/index.html"):
            with open(os.path.join(HERE, "ui.html"), "rb") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8")
        if path == "/api/config":
            path = self.config_path if os.path.exists(self.config_path) else os.path.join(HERE, "example_config.json")
            try:
                with open(path, encoding="utf-8") as f:
                    cfg = json.load(f)
                return self._send(200, {"config": cfg, "loaded_from": shown(path),
                                        "save_to": shown(self.config_path),
                                        "defaults": {"rules": sch.DEFAULT_RULES, "weights": sch.DEFAULT_WEIGHTS},
                                        "restart_needed": code_stamp() != STARTED_WITH})
            except (OSError, json.JSONDecodeError) as exc:
                return self._send(400, {"error": str(exc)})
        if path == "/api/schedule":
            # the last schedule built for a month, so it survives reloads and restarts
            try:
                sched = self.schedule_path(parse_qs(urlsplit(self.path).query).get("month", [""])[0])
                if not os.path.exists(sched):
                    return self._send(200, {"result": None})
                with open(sched, encoding="utf-8") as f:
                    return self._send(200, {"result": json.load(f)})
            except (sch.ConfigError, OSError, json.JSONDecodeError) as exc:
                return self._send(400, {"error": str(exc)})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        i18n.set_lang(self.headers.get("X-Lang", "en"))
        try:
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length) or b"{}")
            path = urlsplit(self.path).path
            if path == "/api/resolve":
                # with a grid: fold it into rules first, so weekly patterns carry over to another month
                cfg = config_from_payload(payload) if "grid" in payload else payload["config"]
                return self._send(200, resolve(cfg, payload.get("month"), os.path.dirname(self.config_path)))
            if path == "/api/solve":
                cfg = config_from_payload(payload)
                seed = payload.get("seed")
                return self._send(200, run_solver(cfg, int(payload.get("iterations", 150_000)),
                                                  int(payload.get("restarts", 3)),
                                                  None if seed in (None, "") else int(seed)))
            if path == "/api/evaluate":
                return self._send(200, evaluate(config_from_payload(payload), payload["slots"]))
            if path == "/api/schedule":
                result = payload["result"]
                sched = self.schedule_path(result["month"])
                write_json(sched, result)
                return self._send(200, {"saved": shown(sched)})
            if path == "/api/export-xlsx":
                result = payload["result"]
                name = f"schedule_{result['month']}.xlsx"
                return self._send(200, build_schedule_xlsx(result),
                                  "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                                  {"Content-Disposition": f'attachment; filename="{name}"'})
            if path == "/api/backup":
                folder = os.path.dirname(self.config_path)
                backup = make_backup(config_from_payload(payload), folder)
                name = f"shift-planner-backup_{dt.date.today().isoformat()}.json"
                return self._send(200, json.dumps(backup, indent=2, ensure_ascii=False).encode(),
                                  "application/json", {"Content-Disposition": f'attachment; filename="{name}"'})
            if path == "/api/restore":
                restore_backup(payload.get("backup"), self.config_path)
                errorlog.log.info("restored a backup into %s", shown(self.config_path))
                return self._send(200, {"restored": shown(self.config_path)})
            if path == "/api/delete-all":
                delete_all_data(self.config_path, payload.get("month"))
                errorlog.log.info("deleted all data in %s", shown(self.config_path))
                return self._send(200, {"deleted": True})
            if path == "/api/client-error":
                # an error in the browser page, reported by the page itself
                err_id = errorlog.record("bug", "page", str(payload.get("message", "?"))[:500],
                                         tb=str(payload.get("stack") or "")[:5000] or None,
                                         context={"where": payload.get("where")})
                return self._send(200, {"id": err_id})
            if path == "/api/save":
                cfg = config_from_payload(payload)
                sch.Problem(cfg)  # validate before writing
                write_json(self.config_path, cfg)  # autosave and the save-on-close beacon may overlap
                return self._send(200, {"saved": shown(self.config_path)})
            self._send(404, {"error": "not found"})
        except sch.ConfigError as exc:
            errorlog.record("input", path, str(exc))
            self._send(400, {"error": str(exc)})
        except (KeyError, ValueError, TypeError) as exc:
            errorlog.record("input", path, f"invalid input: {exc}", exc=exc)
            self._send(400, {"error": _("invalid input: {error}", error=exc)})
        except Exception as exc:
            self._unexpected(exc)


def main():
    ap = argparse.ArgumentParser(description="Browser UI for the gas station scheduler")
    ap.add_argument("--config", default="config.json",
                    help="config file to load and save (falls back to example_config.json on first load)")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    Handler.config_path = os.path.abspath(args.config)
    errorlog.setup()
    ThreadingHTTPServer.daemon_threads = True  # Ctrl+C stops at once, even during a long build
    try:
        server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    except OSError as exc:
        print(f"Cannot start on port {args.port}: {exc.strerror}.\n"
              f"Is the scheduler already running in another terminal? Stop it there with Ctrl+C,\n"
              f"or start this one on another port: python3 ui.py --port {args.port + 1}", file=sys.stderr)
        return 1
    errorlog.log.info("server started on port %s with %s", args.port, shown(Handler.config_path))
    url = f"http://127.0.0.1:{args.port}/"
    print(f"Scheduler UI running at {url}  (Ctrl+C to stop)")
    if not args.no_browser:
        threading.Timer(0.5, webbrowser.open, [url]).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
