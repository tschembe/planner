"""
Translations for server-side texts (messages, problem lists, Excel export).

English texts are the keys; German versions are looked up in DE. The language is kept in a
context variable, so every request of the UI server uses its own language (header X-Lang).

    from i18n import _, day_label
    _("{n} shifts, more than the maximum of {max}", n=23, max=22)
"""

import contextvars

LANG = contextvars.ContextVar("lang", default="en")
LANGUAGES = ("en", "de")

WEEKDAYS = {"en": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
            "de": ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]}
MONTHS = {"en": ["January", "February", "March", "April", "May", "June", "July", "August", "September",
                 "October", "November", "December"],
          "de": ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September",
                 "Oktober", "November", "Dezember"]}

DE = {
    # scheduler.py: input problems
    'Shift "{name}"': "Schicht „{name}“",
    "{where}: start time": "{where}: Beginn",
    "{where}: end time": "{where}: Ende",
    "{where} is empty. Enter a 24-hour time such as 06:00.":
        "{where} ist leer. Bitte eine Uhrzeit im 24-Stunden-Format eingeben, z. B. 06:00.",
    '{where} "{value}" is not a valid time. Use 24-hour HH:MM, e.g. 06:00 or 22:00.':
        "{where} „{value}“ ist keine gültige Uhrzeit. Bitte 24-Stunden-Format HH:MM verwenden, z. B. 06:00 oder 22:00.",
    "There are no shifts yet. Add at least one shift on the Team & shifts tab.":
        "Es gibt noch keine Schichten. Bitte im Tab Team & Schichten mindestens eine Schicht anlegen.",
    "Two shifts have the same name. Give every shift its own name.":
        "Zwei Schichten haben denselben Namen. Bitte jeder Schicht einen eigenen Namen geben.",
    "Night shifts (Rules): unknown shift {name!r}": "Nachtschichten (Regeln): unbekannte Schicht {name!r}",
    "There are no employees yet. Add at least one employee on the Team & shifts tab.":
        "Es gibt noch keine Mitarbeitenden. Bitte im Tab Team & Schichten mindestens eine Person anlegen.",
    'Employee "{name}": unknown contract "{contract}" (use {options})':
        "Person „{name}“: unbekannter Vertrag „{contract}“ (erlaubt: {options})",
    "Two employees have the same name. Give every employee their own name.":
        "Zwei Personen haben denselben Namen. Bitte jeder Person einen eigenen Namen geben.",
    # scheduler.py: problems in a schedule
    "{day} is day {run} in a row (limit {limit})": "{day} ist Tag {run} am Stück (Grenze {limit})",
    "{n} shifts on {day} (limit {limit} per day)": "{n} Schichten am {day} (Grenze {limit} pro Tag)",
    'scheduled on {day} {shift} but marked "must not work"':
        "am {day} für {shift} eingeteilt, obwohl „darf nicht arbeiten“ eingetragen ist",
    "only {h} h rest before {day} {shift} (minimum {min} h)": "nur {h} h Ruhe vor {day} {shift} (mindestens {min} h)",
    "must work {day} {shift} but is not scheduled": "muss am {day} {shift} arbeiten, ist aber nicht eingeteilt",
    "{n} shifts, more than the maximum of {max}": "{n} Schichten, mehr als das Maximum von {max}",
    "{n} shifts, fewer than the minimum of {min}": "{n} Schichten, weniger als das Minimum von {min}",
    "{day} {shift}: {missing} of {need} place not filled ({avail})":
        "{day} {shift}: {missing} von {need} Platz nicht besetzt ({avail})",
    "{day} {shift}: {missing} of {need} places not filled ({avail})":
        "{day} {shift}: {missing} von {need} Plätzen nicht besetzt ({avail})",
    "nobody is available": "niemand ist verfügbar",
    "only 1 person is available": "nur 1 Person ist verfügbar",
    "only {n} people are available": "nur {n} Personen sind verfügbar",
    # ui.py
    "Unexpected error ({id}). Details: python3 errorlog.py --id {id}":
        "Unerwarteter Fehler ({id}). Details: python3 errorlog.py --id {id}",
    "invalid input: {error}": "Ungültige Eingabe: {error}",
    "The availability of {name!r} does not match the month or the shifts. Reload the page.":
        "Die Verfügbarkeit von {name!r} passt nicht zu Monat oder Schichten. Bitte die Seite neu laden.",
    # xlsx_export.py
    "Schedule · {month}": "Dienstplan · {month}",
    "By day": "Nach Tag",
    "By employee": "Nach Person",
    "Summary": "Übersicht",
    "Date": "Datum",
    "Employee": "Person",
    "Total": "Summe",
    "(unfilled)": "(unbesetzt)",
    "Shifts:": "Schichten:",
    "Colours:": "Farben:",
    "Must work": "Muss arbeiten",
    "Can work": "Kann arbeiten",
    "Neutral": "Neutral",
    "Shifts": "Schichten",
    "Fair share": "Fairer Anteil",
    "Weekend shifts": "Wochenendschichten",
    "Weekend fair share": "Fairer Anteil Wochenende",
    "Must work honoured": "Muss arbeiten erfüllt",
    "Must work requested": "Muss arbeiten gewünscht",
    "Problems to check": "Zu prüfende Probleme",
    "None: every shift is staffed and every must / must-not request is honoured.":
        "Keine: jede Schicht ist besetzt und alle Muss-/Darf-nicht-Wünsche sind berücksichtigt.",
    "Score {score} (lower is better)": "Bewertung {score} (niedriger ist besser)",
    # ui.py: backup and restore
    "This file is not a backup of the shift planner.": "Diese Datei ist keine Sicherung des Schichtplaners.",
}


def lang():
    return LANG.get() if LANG.get() in LANGUAGES else "en"


def set_lang(value):
    """Use `value` ('en' or 'de') for the rest of this request / command."""
    LANG.set(value if value in LANGUAGES else "en")


def _(text, **values):
    """Translate an English text into the current language and fill in {placeholders}."""
    if lang() == "de":
        text = DE.get(text, text)
    return text.format(**values) if values else text


def num(x):
    """Number for messages: 11 -> '11', 10.5 -> '10.5' (German: '10,5')."""
    s = f"{x:g}"
    return s.replace(".", ",") if lang() == "de" else s


def weekday_short(date):
    return WEEKDAYS[lang()][date.weekday()]


def day_label(date):
    """Readable date for messages: 'Sat 3 Oct' / 'Sa 3. Okt'."""
    month = MONTHS[lang()][date.month - 1][:3]
    if lang() == "de":
        return f"{weekday_short(date)} {date.day}. {month}"
    return f"{weekday_short(date)} {date.day} {month}"


def month_label(year, month):
    """'October 2026' / 'Oktober 2026'."""
    return f"{MONTHS[lang()][month - 1]} {year}"
