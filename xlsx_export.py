"""
Excel (.xlsx) export of a solved schedule, standard library only.

build_schedule_xlsx(result) takes the schedule dict produced by ui.run_solver and returns
the bytes of a workbook with three sheets: By day, By employee, Summary.
"""

import datetime as dt
import io
import zipfile
from xml.sax.saxutils import escape

from i18n import _, month_label, weekday_short

# ---- styles (indices into cellXfs in STYLES_XML) ------------------------------
BASE, HEADER, TITLE, DATE, DATE_WEEKEND, WEEKEND, EMPTY, MUST, CAN, NEUTRAL, NEVER, BAD, NUM1, HEADER_WEEKEND, WRAP = range(15)
LEVEL_STYLE = {"M": MUST, "C": CAN, "N": NEUTRAL, "X": NEVER}

STYLES_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<numFmts count="2"><numFmt numFmtId="164" formatCode="ddd d mmm"/><numFmt numFmtId="165" formatCode="0.0"/></numFmts>
<fonts count="4">
<font><sz val="11"/><name val="Calibri"/></font>
<font><b/><sz val="11"/><name val="Calibri"/></font>
<font><b/><sz val="14"/><name val="Calibri"/></font>
<font><b/><sz val="11"/><color rgb="FFB3261E"/><name val="Calibri"/></font>
</fonts>
<fills count="9">
<fill><patternFill patternType="none"/></fill>
<fill><patternFill patternType="gray125"/></fill>
<fill><patternFill patternType="solid"><fgColor rgb="FFECEAE6"/></patternFill></fill>
<fill><patternFill patternType="solid"><fgColor rgb="FFFBF3DF"/></patternFill></fill>
<fill><patternFill patternType="solid"><fgColor rgb="FFDBE6FB"/></patternFill></fill>
<fill><patternFill patternType="solid"><fgColor rgb="FFD7F0E0"/></patternFill></fill>
<fill><patternFill patternType="solid"><fgColor rgb="FFE9E7E2"/></patternFill></fill>
<fill><patternFill patternType="solid"><fgColor rgb="FFF9DCD8"/></patternFill></fill>
<fill><patternFill patternType="solid"><fgColor rgb="FFFDE6E3"/></patternFill></fill>
</fills>
<borders count="2">
<border><left/><right/><top/><bottom/><diagonal/></border>
<border><left/><right/><top/><bottom style="thin"><color rgb="FF9A9A9A"/></bottom><diagonal/></border>
</borders>
<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
<cellXfs count="15">
<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>
<xf numFmtId="0" fontId="1" fillId="2" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="center" wrapText="1"/></xf>
<xf numFmtId="0" fontId="2" fillId="0" borderId="0" xfId="0" applyFont="1"/>
<xf numFmtId="164" fontId="1" fillId="0" borderId="0" xfId="0" applyNumberFormat="1" applyFont="1" applyAlignment="1"><alignment horizontal="left" vertical="top"/></xf>
<xf numFmtId="164" fontId="1" fillId="3" borderId="0" xfId="0" applyNumberFormat="1" applyFont="1" applyFill="1" applyAlignment="1"><alignment horizontal="left" vertical="top"/></xf>
<xf numFmtId="0" fontId="0" fillId="3" borderId="0" xfId="0" applyFill="1" applyAlignment="1"><alignment vertical="top" wrapText="1"/></xf>
<xf numFmtId="0" fontId="3" fillId="8" borderId="0" xfId="0" applyFont="1" applyFill="1" applyAlignment="1"><alignment vertical="top" wrapText="1"/></xf>
<xf numFmtId="0" fontId="1" fillId="4" borderId="0" xfId="0" applyFont="1" applyFill="1" applyAlignment="1"><alignment horizontal="center"/></xf>
<xf numFmtId="0" fontId="1" fillId="5" borderId="0" xfId="0" applyFont="1" applyFill="1" applyAlignment="1"><alignment horizontal="center"/></xf>
<xf numFmtId="0" fontId="1" fillId="6" borderId="0" xfId="0" applyFont="1" applyFill="1" applyAlignment="1"><alignment horizontal="center"/></xf>
<xf numFmtId="0" fontId="1" fillId="7" borderId="0" xfId="0" applyFont="1" applyFill="1" applyAlignment="1"><alignment horizontal="center"/></xf>
<xf numFmtId="0" fontId="3" fillId="0" borderId="0" xfId="0" applyFont="1"/>
<xf numFmtId="165" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>
<xf numFmtId="0" fontId="1" fillId="3" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="center" wrapText="1"/></xf>
<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0" applyAlignment="1"><alignment vertical="top" wrapText="1"/></xf>
</cellXfs>
<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>
</styleSheet>
"""


# ---- minimal workbook writer ----------------------------------------------------

def col_letter(i):
    """0 -> A, 25 -> Z, 26 -> AA"""
    s = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def _cell_xml(ref, value, style):
    s = f' s="{style}"' if style else ""
    if value is None or value == "":
        return f'<c r="{ref}"{s}/>' if style else ""
    if isinstance(value, dt.date):
        return f'<c r="{ref}"{s}><v>{(value - dt.date(1899, 12, 30)).days}</v></c>'
    if isinstance(value, bool):
        value = str(value)
    if isinstance(value, (int, float)):
        return f'<c r="{ref}"{s}><v>{value}</v></c>'
    return f'<c r="{ref}"{s} t="inlineStr"><is><t xml:space="preserve">{escape(str(value))}</t></is></c>'


def _sheet_xml(rows, widths, freeze, heights, fit_height=0):
    out = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
           '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
           'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
           '<sheetPr><pageSetUpPr fitToPage="1"/></sheetPr><sheetViews><sheetView workbookViewId="0">']
    if freeze:
        r, c = freeze
        pane = "bottomRight" if r and c else ("bottomLeft" if r else "topRight")
        attrs = (f' xSplit="{c}"' if c else "") + (f' ySplit="{r}"' if r else "")
        out.append(f'<pane{attrs} topLeftCell="{col_letter(c)}{r + 1}" activePane="{pane}" state="frozen"/>'
                   f'<selection pane="{pane}"/>')
    out.append("</sheetView></sheetViews>")
    if widths:
        out.append("<cols>" + "".join(f'<col min="{i + 1}" max="{i + 1}" width="{w}" customWidth="1"/>'
                                      for i, w in enumerate(widths)) + "</cols>")
    out.append("<sheetData>")
    for r, row in enumerate(rows, start=1):
        ht = f' ht="{heights[r]}" customHeight="1"' if r in heights else ""
        cells = []
        for c, cell in enumerate(row):
            value, style = cell if isinstance(cell, tuple) else (cell, BASE)
            cells.append(_cell_xml(f"{col_letter(c)}{r}", value, style))
        out.append(f'<row r="{r}"{ht}>{"".join(cells)}</row>')
    out.append('</sheetData><pageMargins left="0.4" right="0.4" top="0.5" bottom="0.5" header="0.3" footer="0.3"/>'
               # paperSize 9 = A4; fitToHeight 1 = the whole sheet on one page (0 = as many pages as needed)
               f'<pageSetup paperSize="9" orientation="landscape" fitToWidth="1" fitToHeight="{fit_height}"/></worksheet>')
    return "".join(out)


def write_workbook(sheets):
    """sheets: list of dicts {name, rows, widths?, freeze?: (rows, cols), heights?: {row: pt}}.
    A cell is a plain value or a (value, style) tuple. Returns the .xlsx bytes."""
    buf = io.BytesIO()
    n = len(sheets)
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
                   '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                   '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                   '<Default Extension="xml" ContentType="application/xml"/>'
                   '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
                   '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
                   + "".join(f'<Override PartName="/xl/worksheets/sheet{i + 1}.xml" '
                             'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                             for i in range(n))
                   + "</Types>")
        z.writestr("_rels/.rels",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
                   '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
                   "</Relationships>")
        z.writestr("xl/workbook.xml",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
                   '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                   'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>'
                   + "".join(f'<sheet name="{escape(sh["name"])}" sheetId="{i + 1}" r:id="rId{i + 1}"/>'
                             for i, sh in enumerate(sheets))
                   + "</sheets></workbook>")
        z.writestr("xl/_rels/workbook.xml.rels",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
                   '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   + "".join(f'<Relationship Id="rId{i + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
                             f'Target="worksheets/sheet{i + 1}.xml"/>' for i in range(n))
                   + f'<Relationship Id="rId{n + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
                   "</Relationships>")
        z.writestr("xl/styles.xml", STYLES_XML)
        for i, sh in enumerate(sheets):
            z.writestr(f"xl/worksheets/sheet{i + 1}.xml",
                       _sheet_xml(sh["rows"], sh.get("widths"), sh.get("freeze"), sh.get("heights", {}), sh.get("fit_height", 0)))
    return buf.getvalue()


# ---- schedule layout ------------------------------------------------------------

def _abbreviations(names):
    for n in range(1, 5):
        a = [x[:n].upper() for x in names]
        if len(set(a)) == len(a):
            return a
    return [str(i + 1) for i in range(len(names))]


def build_schedule_xlsx(result):
    dates = [dt.date.fromisoformat(d) for d in result["dates"]]
    weekend = result["weekend"]
    shifts = result["shifts"]
    y, m = (int(x) for x in result["month"].split("-"))
    title = _("Schedule · {month}", month=month_label(y, m))
    by_slot = {(sl["d"], sl["s"]): sl for sl in result["slots"]}

    # By day: one row per date, one column per shift
    rows = [[(title, TITLE)],
            [(_("Date"), HEADER)] + [(sh["label"], HEADER) for sh in shifts]]
    for d, date in enumerate(dates):
        row = [(date, DATE_WEEKEND if weekend[d] else DATE)]
        for s in range(len(shifts)):
            sl = by_slot.get((d, s))
            if sl is None:
                row.append(("—", WEEKEND if weekend[d] else WRAP))
                continue
            names = [p["name"] if p else _("(unfilled)") for p in sl["people"]]
            unfilled = any(p is None for p in sl["people"])
            row.append((", ".join(names), EMPTY if unfilled else (WEEKEND if weekend[d] else WRAP)))
        rows.append(row)
    widest = max((len(", ".join(p["name"] if p else _("(unfilled)") for p in sl["people"])) for sl in result["slots"]), default=10)
    by_day = {"name": _("By day"), "rows": rows, "freeze": (2, 1), "heights": {2: 30}, "fit_height": 1,
              "widths": [14] + [min(max(18, widest + 2), 60)] * len(shifts)}

    # By employee: one row per employee, one column per day, cell = shift abbreviation coloured by level
    abbr = _abbreviations([sh["name"] for sh in shifts])
    worked = {}
    for sl in result["slots"]:
        for p in sl["people"]:
            if p:
                worked.setdefault((p["name"], sl["d"]), []).append((sl["s"], p["level"]))
    rows = [[(title, TITLE)],
            [(_("Employee"), HEADER)]
            + [(f"{weekday_short(date)[:2]}\n{date.day}", HEADER_WEEKEND if weekend[d] else HEADER)
               for d, date in enumerate(dates)]
            + [(_("Total"), HEADER)]]
    for name in result["employees"]:
        row, total = [name], 0
        for d in range(len(dates)):
            items = sorted(worked.get((name, d), []))
            total += len(items)
            if not items:
                row.append((None, WEEKEND if weekend[d] else BASE))
            else:
                levels = [lvl for _, lvl in items]
                style = LEVEL_STYLE["M" if "M" in levels else levels[0]]
                row.append(("+".join(abbr[s] for s, _ in items), style))
        row.append(total)
        rows.append(row)
    rows.append([])
    rows.append([(_("Shifts:"), HEADER)] + [" · ".join(f"{abbr[i]} = {sh['label']}" for i, sh in enumerate(shifts))])
    rows.append([(_("Colours:"), HEADER), ("", MUST), _("Must work"), ("", CAN), _("Can work"), ("", NEUTRAL), _("Neutral")])
    by_emp = {"name": _("By employee"), "rows": rows, "freeze": (2, 1), "heights": {2: 30}, "fit_height": 1,
              "widths": [max(12, max((len(n) for n in result["employees"]), default=8) + 2)]
              + [max(5, 2 + max(len(a) for a in abbr))] * len(dates) + [7]}

    # Summary: statistics per employee and the list of rule violations
    rows = [[(title, TITLE)],
            [(_(h), HEADER) for h in ["Employee", "Shifts", "Fair share", "Weekend shifts", "Weekend fair share",
                                      "Must work honoured", "Must work requested", "Can work", "Neutral"]]]
    for r in result["summary"]:
        rows.append([r["name"], r["shifts"], (round(r["target"], 1), NUM1), r["weekend"],
                     (round(r["weekend_target"], 1), NUM1), r["must"], r["must_total"], r["can"], r["neutral"]])
    rows.append([])
    rows.append([(_("Problems to check"), TITLE)])
    if result["problems"]:
        rows += [[(p, BAD)] for p in result["problems"]]
    else:
        rows.append([_("None: every shift is staffed and every must / must-not request is honoured.")])
    rows.append([])
    rows.append([_("Score {score} (lower is better)", score=f"{result['cost']:.1f}")])
    summary = {"name": _("Summary"), "rows": rows, "freeze": (2, 0), "heights": {2: 30},
               "widths": [max(14, max((len(n) for n in result["employees"]), default=8) + 2), 9, 11, 11, 12, 12, 12, 10, 10]}

    return write_workbook([by_day, by_emp, summary])
