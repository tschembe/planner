#!/usr/bin/env python3
"""
Monthly shift scheduler for a gas station.

Reads a JSON config (employees, shifts, availability rules) plus an optional
availability grid CSV, then searches for the best schedule for one month.

Availability levels (per employee, per day and shift):
  must     "must work"         - must be scheduled             (code M)
  can      "can work"          - available and happy to work   (code C)
  neutral  "neutral/flexible"  - available, no preference      (code N)
  never    "must not work"     - must not be scheduled         (code X)

Hard rules (heavily penalised, reported if they cannot be met):
  every slot fully staffed, "must not work" never scheduled, "must work"
  always scheduled, max shifts per day, minimum rest between shifts,
  per-employee monthly maximum, no night shifts for employees with
  "night_shifts": false.
Soft goals (traded off by the weights in the config):
  prefer "can" over "neutral", spread total and weekend shifts fairly,
  respect per-employee minimum shifts and max consecutive working days.

The search is a greedy construction followed by simulated annealing.

Usage:
  python3 scheduler.py solve example_config.json
  python3 scheduler.py solve example_config.json --out october
  python3 scheduler.py template example_config.json -o availability.csv
  python3 scheduler.py solve example_config.json --availability availability.csv
"""

import argparse
import calendar
import csv
import datetime as dt
import json
import math
import os
import random
import re
import sys

from i18n import _, day_label, num, set_lang

MUST, CAN, NEUTRAL, NEVER = 0, 1, 2, 3
LEVEL_CODES = ["M", "C", "N", "X"]
LEVEL_ALIASES = {
    "must": MUST, "must work": MUST, "m": MUST,
    "can": CAN, "can work": CAN, "c": CAN,
    "neutral": NEUTRAL, "flexible": NEUTRAL, "neutral/flexible": NEUTRAL, "n": NEUTRAL,
    "never": NEVER, "must not": NEVER, "must not work": NEVER, "x": NEVER,
}
WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

DEFAULT_WEIGHTS = {
    "hard": 10000,            # any hard-rule violation
    "unfilled": 1000,         # per empty position in a slot
    "can": 0,                 # per shift on a "can work" slot
    "neutral": 2,             # per shift on a "neutral" slot
    "fairness": 3,            # * (shifts - fair share)^2
    "weekend_fairness": 3,    # * (weekend shifts - fair share)^2
    "below_min_shifts": 50,   # per shift under an employee's min_shifts
    "over_consecutive": 30,   # per day beyond max_consecutive_days
}
# contract type -> fair-share weight
CONTRACTS = {"full-time": 1.0, "part-time": 0.5, "mini-job": 0.25}


def contract_of(emp):
    """The employee's contract; older configs with only an "fte" number get the nearest contract."""
    if "contract" in emp:
        return emp["contract"]
    fte = emp.get("fte")
    if fte is None:
        return "full-time"
    return "full-time" if fte >= 0.75 else "part-time" if fte >= 0.375 else "mini-job"


DEFAULT_RULES = {
    "min_rest_hours": 11,
    "max_consecutive_days": 6,
    "max_shifts_per_day": 1,
    "weekend": ["Sat", "Sun"],
}


class ConfigError(Exception):
    pass


def parse_level(value, where):
    level = LEVEL_ALIASES.get(str(value).strip().lower())
    if level is None:
        raise ConfigError(f"{where}: unknown availability {value!r} "
                          f"(use must / can / neutral / never or M / C / N / X)")
    return level


def parse_weekday(value, where):
    key = str(value).strip().lower()[:3]
    if key not in WEEKDAYS:
        raise ConfigError(f"{where}: unknown weekday {value!r}")
    return WEEKDAYS.index(key)


def parse_time(value, where):
    """'HH:MM' (24-hour) -> hours as float. `where` names the field, e.g. 'Shift "morgen": start time'."""
    if value in (None, ""):
        raise ConfigError(_("{where} is empty. Enter a 24-hour time such as 06:00.", where=where))
    try:
        h, m = (int(x) for x in str(value).split(":"))
    except ValueError:
        h = m = -1
    if not (0 <= h <= 24 and 0 <= m < 60) or (h == 24 and m):
        raise ConfigError(_('{where} "{value}" is not a valid time. Use 24-hour HH:MM, e.g. 06:00 or 22:00.',
                              where=where, value=value))
    return h + m / 60


def parse_date(value, where):
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        raise ConfigError(f"{where}: bad date {value!r}, expected YYYY-MM-DD") from None


def water_fill(total, weight, cap, floor):
    """Split `total` in proportion to `weight`, keeping every part within [floor, cap].
    Parts that hit a bound are fixed there and the rest is re-split among the others."""
    n = len(weight)
    part = [0.0] * n
    free = set(range(n))
    while free:
        remaining = total - sum(part[i] for i in range(n) if i not in free)
        wsum = sum(weight[i] for i in free)
        for i in free:
            part[i] = remaining * weight[i] / wsum if wsum > 0 else 0.0
        over = [i for i in free if part[i] > cap[i] + 1e-9]
        under = [i for i in free if part[i] < floor[i] - 1e-9]
        if not over and not under:
            break
        for i in (over or under):  # fix one side at a time, then re-split
            part[i] = cap[i] if over else floor[i]
            free.discard(i)
    return part


class Problem:
    def __init__(self, cfg, month=None, availability_csv=None):
        month = month or cfg.get("month")
        if not month:
            raise ConfigError("no month given (config 'month' or --month YYYY-MM)")
        year, mon = (int(x) for x in month.split("-"))
        self.month = f"{year:04d}-{mon:02d}"
        self.dates = [dt.date(year, mon, d) for d in range(1, calendar.monthrange(year, mon)[1] + 1)]
        self.D = len(self.dates)

        self.w = {**DEFAULT_WEIGHTS, **cfg.get("weights", {})}
        rules = {**DEFAULT_RULES, **cfg.get("rules", {})}
        self.min_rest = rules["min_rest_hours"]
        self.max_per_day = rules["max_shifts_per_day"]
        weekend_days = {parse_weekday(x, "rules.weekend") for x in rules["weekend"]}
        self.is_weekend = [d.weekday() in weekend_days for d in self.dates]

        self._parse_shifts(cfg.get("shifts") or [], rules.get("night_shifts"))
        self._parse_employees(cfg.get("employees") or [], rules)
        self._parse_availability(cfg.get("availability", {}))
        if availability_csv:
            self.load_availability_csv(availability_csv)
        self._apply_night_flags()
        self._build_slots()
        self._compute_targets()

    # ---- parsing -------------------------------------------------------

    def _parse_shifts(self, shifts, night_shifts=None):
        if not shifts:
            raise ConfigError(_("There are no shifts yet. Add at least one shift on the Team & shifts tab."))
        self.shift_names, self.start, self.dur, self.shift_label, self.is_night = [], [], [], [], []
        self.base_need = []  # [s][weekday]
        for i, sh in enumerate(shifts):
            name = sh.get("name") or f"#{i + 1}"
            where = _('Shift "{name}"', name=name)
            start = parse_time(sh.get("start"), _("{where}: start time", where=where))
            end = parse_time(sh.get("end"), _("{where}: end time", where=where))
            dur = end - start if end > start else end - start + 24
            need = [int(sh.get("staff", 1))] * 7
            for wd, n in sh.get("staff_by_weekday", {}).items():
                need[parse_weekday(wd, where)] = int(n)
            self.shift_names.append(name)
            self.start.append(start)
            self.dur.append(dur)
            self.shift_label.append(f"{name} {sh['start']}-{sh['end']}")
            self.base_need.append(need)
            if night_shifts is not None:
                # rules.night_shifts lists the night shifts by name
                self.is_night.append(name in night_shifts)
            else:
                # otherwise: explicit "night" flag, else a shift running past midnight (ending at 00:00
                # does not count) or one named "night"/"nacht"
                overnight = end < start and end > 0
                self.is_night.append(bool(sh.get("night", overnight or re.search(r"night|nacht", name, re.I))))
        self.S = len(self.shift_names)
        if len(set(self.shift_names)) != self.S:
            raise ConfigError(_("Two shifts have the same name. Give every shift its own name."))
        for name in night_shifts or []:
            if name not in self.shift_names:
                raise ConfigError(_("Night shifts (Rules): unknown shift {name!r}", name=name))

    def _parse_employees(self, employees, rules):
        if not employees:
            raise ConfigError(_("There are no employees yet. Add at least one employee on the Team & shifts tab."))
        self.names, self.max_shifts, self.min_shifts, self.share, self.max_consec = [], [], [], [], []
        self.works_nights = []
        for emp in employees:
            if isinstance(emp, str):
                emp = {"name": emp}
            self.names.append(emp["name"])
            self.max_shifts.append(emp.get("max_shifts", 10 ** 6))
            self.min_shifts.append(emp.get("min_shifts", 0))
            contract = contract_of(emp)
            if contract not in CONTRACTS:
                raise ConfigError(_('Employee "{name}": unknown contract "{contract}" (use {options})',
                                    name=emp["name"], contract=contract, options=", ".join(CONTRACTS)))
            self.share.append(CONTRACTS[contract])  # fair-share weight
            # one limit for everybody, from the rules
            self.max_consec.append(rules["max_consecutive_days"])
            self.works_nights.append(bool(emp.get("night_shifts", True)))
        self.E = len(self.names)
        if len(set(self.names)) != self.E:
            raise ConfigError(_("Two employees have the same name. Give every employee their own name."))

    def _shift_index(self, name, where):
        if name not in self.shift_names:
            raise ConfigError(f"{where}: unknown shift {name!r} (known: {', '.join(self.shift_names)})")
        return self.shift_names.index(name)

    def _dates_in(self, key, where):
        """A single date or an inclusive range 'YYYY-MM-DD..YYYY-MM-DD', clipped to the month."""
        if ".." in key:
            a, b = (parse_date(x.strip(), where) for x in key.split("..", 1))
        else:
            a = b = parse_date(key, where)
        return [d for d in range(self.D) if a <= self.dates[d] <= b]

    def _parse_availability(self, avail):
        """Resolve rules; the most specific one wins:
        date_shifts > dates > weekday_shifts > weekdays > shifts > default."""
        self.pref = [[[NEUTRAL] * self.S for _ in range(self.D)] for _ in range(self.E)]
        for name in avail:
            if name not in self.names:
                raise ConfigError(f"availability: unknown employee {name!r}")
        for e, name in enumerate(self.names):
            r = avail.get(name, {})
            where = f"availability[{name}]"
            grid = self.pref[e]
            default = parse_level(r.get("default", "neutral"), where)
            for d in range(self.D):
                grid[d] = [default] * self.S
            for sh, lvl in r.get("shifts", {}).items():
                s, lvl = self._shift_index(sh, where), parse_level(lvl, where)
                for d in range(self.D):
                    grid[d][s] = lvl
            for wd, lvl in r.get("weekdays", {}).items():
                w, lvl = parse_weekday(wd, where), parse_level(lvl, where)
                for d in range(self.D):
                    if self.dates[d].weekday() == w:
                        grid[d] = [lvl] * self.S
            for key, lvl in r.get("weekday_shifts", {}).items():
                wd, sh = self._split_key(key, where)
                w, s, lvl = parse_weekday(wd, where), self._shift_index(sh, where), parse_level(lvl, where)
                for d in range(self.D):
                    if self.dates[d].weekday() == w:
                        grid[d][s] = lvl
            for key, lvl in r.get("dates", {}).items():
                lvl = parse_level(lvl, where)
                for d in self._dates_in(key, where):
                    grid[d] = [lvl] * self.S
            for key, lvl in r.get("date_shifts", {}).items():
                date, sh = self._split_key(key, where)
                s, lvl = self._shift_index(sh, where), parse_level(lvl, where)
                for d in self._dates_in(date, where):
                    grid[d][s] = lvl

    def _apply_night_flags(self):
        """Employees with "night_shifts": false are never scheduled on night shifts."""
        nights = [s for s in range(self.S) if self.is_night[s]]
        for e in range(self.E):
            if not self.works_nights[e]:
                for day in self.pref[e]:
                    for s in nights:
                        day[s] = NEVER

    @staticmethod
    def _split_key(key, where):
        parts = key.split(None, 1)
        if len(parts) != 2:
            raise ConfigError(f"{where}: key {key!r} must look like '<day> <shift>'")
        return parts

    def csv_columns(self):
        return [(d, s, f"{self.dates[d].isoformat()} {self.shift_names[s]}")
                for d in range(self.D) for s in range(self.S)]

    def load_availability_csv(self, path):
        """Grid with one row per employee and one column per '<date> <shift>' (or '<date>'
        for the whole day). Cells hold M / C / N / X; blank cells keep the config rules."""
        with open(path, newline="") as f:
            rows = list(csv.reader(f))
        if not rows:
            return
        header = rows[0]
        cols = []
        for c, title in enumerate(header[1:], start=1):
            parts = title.split(None, 1)
            if not parts:
                continue
            days = self._dates_in(parts[0], f"{path} column {title!r}")
            shifts = [self._shift_index(parts[1], f"{path}")] if len(parts) == 2 else list(range(self.S))
            cols.append((c, days, shifts))
        for r, row in enumerate(rows[1:], start=2):
            if not row or not row[0].strip():
                continue
            name = row[0].strip()
            if name not in self.names:
                raise ConfigError(f"{path} row {r}: unknown employee {name!r}")
            e = self.names.index(name)
            for c, days, shifts in cols:
                cell = row[c].strip() if c < len(row) else ""
                if not cell:
                    continue
                lvl = parse_level(cell, f"{path} row {r} column {header[c]!r}")
                for d in days:
                    for s in shifts:
                        self.pref[e][d][s] = lvl

    # ---- derived data --------------------------------------------------

    def _build_slots(self):
        self.slots, self.need = [], []
        for d in range(self.D):
            wd = self.dates[d].weekday()
            for s in range(self.S):
                n = self.base_need[s][wd]
                if n > 0:
                    self.slots.append((d, s))
                    self.need.append(n)
        self.K = len(self.slots)
        self.eligible = [[e for e in range(self.E) if self.pref[e][d][s] != NEVER]
                         for d, s in self.slots]
        self.must_count = [sum(row.count(MUST) for row in self.pref[e]) for e in range(self.E)]
        w = self.w
        self.pref_cost = [0, w["can"], w["neutral"], w["hard"]]

    def _compute_targets(self):
        """Fair share of all shifts (and of weekend shifts) per employee.

        Shares are proportional to the contract and to how many days the employee is available,
        but never above what the person can actually work (their max shifts, their available days,
        the days-in-a-row limit) and never below their min shifts. What a capped person cannot take
        is spread over the others (water-filling), so no target is impossible to reach."""
        k = max(1, self.max_consec[0]) if self.E else 1
        per_day = max(1, self.max_per_day)

        def split(day_filter, use_limits):
            days = [d for d in range(self.D) if day_filter(d)]
            demand = sum(n for (d, _), n in zip(self.slots, self.need) if day_filter(d))
            if not days:
                return [0.0] * self.E
            avail = [sum(1 for d in days if any(l != NEVER for l in self.pref[e][d])) for e in range(self.E)]
            weight = [self.share[e] * avail[e] / len(days) for e in range(self.E)]
            cap = [avail[e] * per_day for e in range(self.E)]
            floor = [0.0] * self.E
            if use_limits:
                # with at most k working days in a row, at most D - D // (k + 1) days can be worked
                cap = [min(cap[e], (self.D - self.D // (k + 1)) * per_day, self.max_shifts[e]) for e in range(self.E)]
                floor = [min(self.min_shifts[e], cap[e]) for e in range(self.E)]
            return water_fill(demand, weight, cap, floor)

        self.target = split(lambda d: True, True)
        self.wtarget = split(lambda d: self.is_weekend[d], False)

    # ---- cost ----------------------------------------------------------

    def emp_cost(self, e, days, issues=None):
        """Cost of one employee's month. days[d] = list of shift indices worked on day d."""
        w, pref, pc = self.w, self.pref[e], self.pref_cost
        hard = w["hard"]
        cost = 0.0
        total = weekend = run = musts = 0
        prev_end = None
        for d in range(self.D):
            ss = days[d]
            if not ss:
                run = 0
                continue
            run += 1
            if run > self.max_consec[e]:
                cost += w["over_consecutive"]
                if issues is not None:
                    issues.append(_("{day} is day {run} in a row (limit {limit})",
                                    day=day_label(self.dates[d]), run=run, limit=self.max_consec[e]))
            if len(ss) > 1:
                if len(ss) > self.max_per_day:
                    cost += hard * (len(ss) - self.max_per_day)
                    if issues is not None:
                        issues.append(_("{n} shifts on {day} (limit {limit} per day)",
                                        n=len(ss), day=day_label(self.dates[d]), limit=self.max_per_day))
                ss = sorted(ss, key=self.start.__getitem__)
            for s in ss:
                lvl = pref[d][s]
                cost += pc[lvl]
                if lvl == MUST:
                    musts += 1
                elif lvl == NEVER and issues is not None:
                    issues.append(_('scheduled on {day} {shift} but marked "must not work"',
                                    day=day_label(self.dates[d]), shift=self.shift_names[s]))
                st = d * 24 + self.start[s]
                if prev_end is not None and st - prev_end < self.min_rest:
                    cost += hard
                    if issues is not None:
                        issues.append(_("only {h} h rest before {day} {shift} (minimum {min} h)",
                                        h=num(st - prev_end), day=day_label(self.dates[d]),
                                        shift=self.shift_names[s], min=num(self.min_rest)))
                prev_end = st + self.dur[s]
            total += len(ss)
            if self.is_weekend[d]:
                weekend += len(ss)
        missed = self.must_count[e] - musts
        if missed:
            cost += hard * missed
            if issues is not None:
                for d in range(self.D):
                    for s in range(self.S):
                        if pref[d][s] == MUST and s not in days[d]:
                            issues.append(_("must work {day} {shift} but is not scheduled",
                                            day=day_label(self.dates[d]), shift=self.shift_names[s]))
        if total > self.max_shifts[e]:
            cost += hard * (total - self.max_shifts[e])
            if issues is not None:
                issues.append(_("{n} shifts, more than the maximum of {max}", n=total, max=self.max_shifts[e]))
        if total < self.min_shifts[e]:
            cost += w["below_min_shifts"] * (self.min_shifts[e] - total)
            if issues is not None:
                issues.append(_("{n} shifts, fewer than the minimum of {min}", n=total, min=self.min_shifts[e]))
        cost += w["fairness"] * (total - self.target[e]) ** 2
        cost += w["weekend_fairness"] * (weekend - self.wtarget[e]) ** 2
        return cost


class Solver:
    def __init__(self, p, seed=None):
        self.p = p
        self.rng = random.Random(seed)
        self.assign = [[-1] * n for n in p.need]
        self.days = [[[] for _ in range(p.D)] for _ in range(p.E)]
        self.ecost = [p.emp_cost(e, self.days[e]) for e in range(p.E)]
        self.unfilled = sum(p.need)

    def total(self):
        return sum(self.ecost) + self.p.w["unfilled"] * self.unfilled

    def _set(self, k, i, new):
        """Put employee `new` (or -1) into position i of slot k; returns the cost delta."""
        p = self.p
        d, s = p.slots[k]
        old = self.assign[k][i]
        delta = 0.0
        if old == -1:
            self.unfilled -= 1
            delta -= p.w["unfilled"]
        else:
            self.days[old][d].remove(s)
            c = p.emp_cost(old, self.days[old])
            delta += c - self.ecost[old]
            self.ecost[old] = c
        if new == -1:
            self.unfilled += 1
            delta += p.w["unfilled"]
        else:
            self.days[new][d].append(s)
            c = p.emp_cost(new, self.days[new])
            delta += c - self.ecost[new]
            self.ecost[new] = c
        self.assign[k][i] = new
        return delta

    def greedy(self):
        """Fill the scarcest slots first, each position with the cheapest employee."""
        p, rng = self.p, self.rng
        # "must work" requests go in first so later picks cannot block them
        for k, (d, s) in enumerate(p.slots):
            musts = [e for e in p.eligible[k] if p.pref[e][d][s] == MUST]
            for i, e in enumerate(musts[:p.need[k]]):
                self._set(k, i, e)
        order = sorted(range(p.K), key=lambda k: (len(p.eligible[k]) / p.need[k], rng.random()))
        for k in order:
            d, s = p.slots[k]
            for i in range(p.need[k]):
                if self.assign[k][i] != -1:
                    continue
                best, best_delta = -1, 0.0
                for e in p.eligible[k]:
                    if e in self.assign[k]:
                        continue
                    self.days[e][d].append(s)
                    delta = p.emp_cost(e, self.days[e]) - self.ecost[e] - p.w["unfilled"]
                    self.days[e][d].pop()
                    if delta < best_delta or (delta == best_delta and best != -1 and rng.random() < 0.5):
                        best, best_delta = e, delta
                if best != -1:
                    self._set(k, i, best)

    def _move(self):
        """Random neighbour move. Returns (delta, undo) or None if the move is not applicable."""
        p, rng, assign = self.p, self.rng, self.assign
        k = rng.randrange(p.K)
        i = rng.randrange(p.need[k])
        if rng.random() < 0.5:
            # replace: someone else (or nobody) takes this position
            old = assign[k][i]
            cand = p.eligible[k]
            new = -1 if not cand or rng.random() < 0.02 else cand[rng.randrange(len(cand))]
            if new == old or (new != -1 and new in assign[k]):
                return None
            return self._set(k, i, new), lambda: self._set(k, i, old)
        # swap: two employees trade shifts on different days
        k2 = rng.randrange(p.K)
        if p.slots[k2][0] == p.slots[k][0]:
            return None
        j = rng.randrange(p.need[k2])
        e1, e2 = assign[k][i], assign[k2][j]
        if e1 == -1 or e2 == -1 or e1 == e2 or e1 in assign[k2] or e2 in assign[k]:
            return None
        d1, s1 = p.slots[k]
        d2, s2 = p.slots[k2]
        if p.pref[e1][d2][s2] == NEVER or p.pref[e2][d1][s1] == NEVER:
            return None
        delta = self._set(k, i, -1) + self._set(k2, j, e1) + self._set(k, i, e2)

        def undo():
            self._set(k, i, -1)
            self._set(k2, j, e2)
            self._set(k, i, e1)
        return delta, undo

    def anneal(self, iterations, t_start=30.0, t_end=0.05):
        cur = self.total()
        best, best_assign = cur, [list(a) for a in self.assign]
        ratio = t_end / t_start
        for it in range(iterations):
            t = t_start * ratio ** (it / iterations)
            m = self._move()
            if m is None:
                continue
            delta, undo = m
            if delta <= 1e-9 or self.rng.random() < math.exp(-delta / t):
                cur += delta
                if cur < best - 1e-9:
                    best, best_assign = cur, [list(a) for a in self.assign]
            else:
                undo()
        self.load(best_assign)
        return best

    def load(self, assign):
        p = self.p
        self.assign = [list(a) for a in assign]
        self.days = [[[] for _ in range(p.D)] for _ in range(p.E)]
        self.unfilled = 0
        for k, (d, s) in enumerate(p.slots):
            for e in self.assign[k]:
                if e == -1:
                    self.unfilled += 1
                else:
                    self.days[e][d].append(s)
        self.ecost = [p.emp_cost(e, self.days[e]) for e in range(p.E)]


def solve(p, iterations, restarts, seed, log=sys.stderr):
    best = None
    for r in range(restarts):
        sv = Solver(p, None if seed is None else seed + r)
        sv.greedy()
        start = sv.total()
        cost = sv.anneal(iterations)
        print(f"run {r + 1}/{restarts}: greedy {start:.1f} -> annealed {cost:.1f}", file=log)
        if best is None or cost < best.total():
            best = sv
    return best


# ---- output ----------------------------------------------------------------

def print_schedule(p, sv, out=sys.stdout):
    by_slot = {p.slots[k]: [p.names[e] if e != -1 else "(EMPTY)" for e in sv.assign[k]] for k in range(p.K)}
    rows = []
    for d, date in enumerate(p.dates):
        cells = [", ".join(by_slot.get((d, s), ["-"])) for s in range(p.S)]
        mark = "*" if p.is_weekend[d] else " "
        rows.append([f"{date:%a %d}{mark}"] + cells)
    header = ["Date"] + p.shift_label
    widths = [max(len(r[c]) for r in rows + [header]) for c in range(len(header))]
    line = "-+-".join("-" * w for w in widths)
    print(f"\nSchedule for {p.month}   (* = weekend)\n", file=out)
    print(" | ".join(h.ljust(w) for h, w in zip(header, widths)), file=out)
    print(line, file=out)
    for r in rows:
        print(" | ".join(c.ljust(w) for c, w in zip(r, widths)), file=out)


def summarize(p, sv):
    """Per-employee statistics and the list of rule violations for a solved schedule."""
    rows = []
    for e in range(p.E):
        counts = [0, 0, 0, 0]
        weekend = 0
        for d in range(p.D):
            for s in sv.days[e][d]:
                counts[p.pref[e][d][s]] += 1
                weekend += p.is_weekend[d]
        rows.append({"name": p.names[e], "shifts": sum(counts), "target": p.target[e],
                     "weekend": weekend, "weekend_target": p.wtarget[e],
                     "must": counts[MUST], "must_total": p.must_count[e],
                     "can": counts[CAN], "neutral": counts[NEUTRAL]})

    problems = []
    for k, (d, s) in enumerate(p.slots):
        missing = sv.assign[k].count(-1)
        if missing:
            avail = len(p.eligible[k])
            who = (_("nobody is available") if avail == 0 else _("only 1 person is available") if avail == 1
                   else _("only {n} people are available", n=avail))
            text = ("{day} {shift}: {missing} of {need} place not filled ({avail})" if p.need[k] == 1
                    else "{day} {shift}: {missing} of {need} places not filled ({avail})")
            problems.append(_(text, day=day_label(p.dates[d]), shift=p.shift_names[s], missing=missing,
                              need=p.need[k], avail=who))
    for e in range(p.E):
        issues = []
        p.emp_cost(e, sv.days[e], issues)
        problems += [f"{p.names[e]}: {msg}" for msg in issues]
    return rows, problems


def print_summary(p, sv, out=sys.stdout):
    stats, problems = summarize(p, sv)
    print("\nPer employee", file=out)
    header = ["Employee", "Shifts", "Fair share", "Weekend", "Must", "Can", "Neutral"]
    rows = [[r["name"], str(r["shifts"]), f"{r['target']:.1f}",
             f"{r['weekend']} ({r['weekend_target']:.1f})", f"{r['must']}/{r['must_total']}",
             str(r["can"]), str(r["neutral"])] for r in stats]
    widths = [max(len(r[c]) for r in rows + [header]) for c in range(len(header))]
    print("  ".join(h.ljust(w) for h, w in zip(header, widths)), file=out)
    for r in rows:
        print("  ".join(c.ljust(w) for c, w in zip(r, widths)), file=out)

    print(f"\nTotal cost: {sv.total():.1f}", file=out)
    if problems:
        print(f"\n{len(problems)} rule violation(s) / warning(s):", file=out)
        for msg in problems:
            print(f"  - {msg}", file=out)
    else:
        print("All hard rules satisfied.", file=out)


def write_csvs(p, sv, prefix):
    with open(f"{prefix}_by_shift.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "weekday", "shift", "start_end", "employees"])
        for k, (d, s) in enumerate(p.slots):
            names = [p.names[e] if e != -1 else "(EMPTY)" for e in sv.assign[k]]
            w.writerow([p.dates[d].isoformat(), f"{p.dates[d]:%a}", p.shift_names[s],
                        p.shift_label[s].split(" ", 1)[1], "; ".join(names)])
    with open(f"{prefix}_by_employee.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["employee"] + [f"{d:%d %a}" for d in p.dates] + ["total"])
        for e in range(p.E):
            cells = ["+".join(p.shift_names[s] for s in sorted(sv.days[e][d])) for d in range(p.D)]
            w.writerow([p.names[e]] + cells + [sum(len(x) for x in sv.days[e])])
    return [f"{prefix}_by_shift.csv", f"{prefix}_by_employee.csv"]


def write_template(p, path):
    cols = p.csv_columns()
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["employee"] + [c[2] for c in cols])
        for e in range(p.E):
            w.writerow([p.names[e]] + [LEVEL_CODES[p.pref[e][d][s]] for d, s, _ in cols])


# ---- CLI -------------------------------------------------------------------

def load_config(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Monthly gas station shift scheduler")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("solve", help="build the schedule")
    s.add_argument("config")
    s.add_argument("--month", help="YYYY-MM, overrides the config")
    s.add_argument("--availability", help="availability grid CSV (see 'template')")
    s.add_argument("--iterations", type=int, default=150_000)
    s.add_argument("--restarts", type=int, default=3)
    s.add_argument("--seed", type=int)
    s.add_argument("--out", help="write <OUT>_by_shift.csv and <OUT>_by_employee.csv")
    s.add_argument("--lang", choices=["en", "de"], default="en", help="language of the problem messages")

    t = sub.add_parser("template", help="write an availability grid CSV to fill in")
    t.add_argument("config")
    t.add_argument("--month")
    t.add_argument("-o", "--output", default="availability.csv")

    args = ap.parse_args(argv)
    set_lang(getattr(args, "lang", "en"))
    try:
        cfg = load_config(args.config)
        avail_csv = getattr(args, "availability", None) or cfg.get("availability_csv")
        if avail_csv and not os.path.isabs(avail_csv) and not getattr(args, "availability", None):
            avail_csv = os.path.join(os.path.dirname(os.path.abspath(args.config)), avail_csv)
        if args.cmd == "template":
            p = Problem(cfg, args.month, avail_csv)
            write_template(p, args.output)
            print(f"wrote {args.output}: fill cells with M (must work), C (can work), "
                  f"N (neutral), X (must not work)")
            return 0
        p = Problem(cfg, args.month, avail_csv)
    except (ConfigError, OSError, KeyError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    sv = solve(p, args.iterations, args.restarts, args.seed)
    print_schedule(p, sv)
    print_summary(p, sv)
    if args.out:
        for path in write_csvs(p, sv, args.out):
            print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
