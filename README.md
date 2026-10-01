# Gas station monthly scheduler

Pure Python 3 (no dependencies). Builds a month of shift assignments from each
employee's availability and optimises it.

## Use it online

**https://tschembe.github.io/planner/**: the same app, nothing to install. The Python code runs in the
browser ([Pyodide](https://pyodide.org), `webworker.js`; the first visit downloads about 10 MB), and the data
stays in that browser's storage: nothing is uploaded. Each browser and device has its own data, and clearing
the browser's site data deletes it, so use **Export backup** regularly and **Import backup** to move to another
device. Building a schedule takes a little longer than with `ui.py`. The site is served by GitHub Pages from
the `main` branch (`index.html` forwards to `ui.html`).

## Browser UI

```bash
python3 ui.py                    # opens http://127.0.0.1:8765
python3 ui.py --config my.json   # load/save a different file
```

1. **Team & shifts**: employees (max shifts and a **Works night shifts** checkbox each; a **Contract** each
   (Full-time / Part-time / Mini-job); min shifts under **More options**), shifts in the order you want them shown (↑/↓ to reorder), times, staff per weekday; rules, weights.
2. **Availability**: either **One employee** (month calendar) or **Whole team, one shift** (everyone × every day,
   with an available/needed row; click a name or a day to fill a row or column). Pick an employee (night shifts are blocked for anyone without **Works night shifts** in the
   Employees table on tab 1), pick a level (keys 1-4), click or drag across shifts; click a
   date for the whole day or a weekday heading for every such day. The coverage check flags slots
   that can't be staffed before you solve. **Staffing needs** below it shows shifts needed vs. what the team can
   work, which shifts lack people (and on which days), and roughly how many more full-time, part-time or
   mini-job employees that means.
3. **Schedule**: build it (Quick / Balanced / Thorough); click any shift to change who works it (rules are re-checked); view it as a month calendar (all days at once), by day or by employee, click a person
   to highlight them, **Export to Excel** (`schedule_YYYY-MM.xlsx` with By day / By employee / Summary
   sheets, colour-coded, A4 landscape with each schedule sheet on one page), export CSV or **Print** (only the plan, on one
   A4 landscape page, black on white; it is scaled down automatically when a month has many names).

**Settings** (☰ at the top right): language **Deutsch / English** (default: the browser's language) and colour theme
Auto (follows the system), Light or Dark; both are remembered in the browser. Messages from the server, the problem
list and the Excel export follow the chosen language; the command line has `--lang de`.
**History** (☰ menu): every change since the page was opened, newest first (e.g. "Alex: max shifts",
"Built the schedule"); click one to go back to before it, or use **Undo last change** / Ctrl+Z.
**Data** (☰ menu): **Export backup** downloads `shift-planner-backup_YYYY-MM-DD.json` with everything the app keeps
(employees, shifts, availability, rules, weights and every saved `schedule_YYYY-MM.json`). **Import backup** replaces
all of it with such a file (a plain `config.json` is accepted too; then the saved schedules are removed). **Delete all
data** leaves an empty config (no employees or shifts) and removes every saved schedule. Import and delete ask first
and cannot be undone, so export a backup before them.
Each tab starts with a step bar (Step 1 of 3 …) and a Next button. Shift times are typed as 24-hour HH:MM.
The last schedule of each month is kept in `schedule_YYYY-MM.json` next to the config and reopens on start
(marked out of date if the inputs changed since it was built).

Every change is saved automatically to `config.json` in this folder (or the `--config` file) and loaded
again next time; the very first start falls back to `example_config.json`, which is never overwritten.
If the input is invalid (e.g. an empty shift time) the header shows "Not saved" and the reason.
Availability is stored as compact rules, and changing the month carries weekly patterns over.

## Command line

```bash
python3 scheduler.py solve example_config.json                   # print schedule + summary
python3 scheduler.py solve example_config.json --out october     # also write CSVs
python3 scheduler.py template example_config.json -o avail.csv   # availability grid to fill in
python3 scheduler.py solve example_config.json --availability avail.csv
```

## Input

`example_config.json` shows every option:

- **employees**: `name`, optional `max_shifts`, `min_shifts`, `contract` (`full-time` = share 1, `part-time` = ½,
  `mini-job` = ¼ of the shifts; default full-time; an old `fte` number is read as the nearest contract),
  `night_shifts` (default `true`; `false` = never scheduled on a night shift, whatever the availability says)
- **shifts**: `name`, `start`, `end` (`HH:MM`; an end before the start means overnight), `staff`, optional `staff_by_weekday`,
  `night` (defaults to true for shifts that cross midnight or are named "night")
- **rules**: `min_rest_hours`, `max_consecutive_days` (applies to every employee), `max_shifts_per_day`, `weekend` days,
  `night_shifts` (list of shift names that count as night shifts; chosen in the UI under Rules)
- **availability** per employee, one of `must` / `can` / `neutral` / `never`
  (i.e. must work, can work, neutral/flexible, must not work). Rules, most specific wins:
  `date_shifts` ("2026-10-07 Morning") > `dates` ("2026-10-12" or "2026-10-12..2026-10-18")
  > `weekday_shifts` ("Sat Night") > `weekdays` ("Sun") > `shifts` ("Night") > `default`.
- **Availability CSV** (optional, overrides rules): one row per employee, one column per
  `<date> <shift>` (or `<date>` for the whole day), cells `M` / `C` / `N` / `X`; blank = keep rule.
  Generate it with `template`, edit it in a spreadsheet, pass it with `--availability`.
- **weights** (optional): tune the soft-goal trade-offs, see `DEFAULT_WEIGHTS` in `scheduler.py`.

## What "optimal" means

Hard rules: every slot staffed, nobody works a "must not work" slot, every "must work" is honoured,
max shifts per day, minimum rest between shifts, monthly maximum. Anything that cannot be met is
listed under "problems to check" instead of failing silently.

Soft goals: prefer "can work" over "neutral", spread shifts and weekend shifts in proportion to
each person's contract and availability, respect minimum shifts and max consecutive days.

Search: "must work" requests first, then a greedy fill and simulated annealing
(`--iterations`, `--restarts`, `--seed`); the lowest-cost run wins.

## After an update, and when something goes wrong

- After the program files change, **restart the UI** (Ctrl+C in its terminal, then `python3 ui.py`) and reload
  the page; the page shows a red banner while an old server is still running.
- Starting `ui.py` a second time on the same port prints how to stop the first one or use `--port`.
- Unexpected errors in the server or the page are logged in `logs/` (created on first use) with an id such as
  `E-3f9a2c`, which is also shown on screen. View them with `python3 errorlog.py` (last 20),
  `python3 errorlog.py --bugs` or `python3 errorlog.py --id E-3f9a2c`.
