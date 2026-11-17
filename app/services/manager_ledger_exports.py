"""Dependency-free CSV, Excel Open XML and paginated PDF ledger downloads."""
import csv
import io
import re
from datetime import datetime
from decimal import Decimal
from xml.etree.ElementTree import Element, SubElement, tostring
from zipfile import ZIP_DEFLATED, ZipFile

from app.services.table_availability import RESTAURANT_TIMEZONE

HEADERS = ["Date / time (IST)", "Entry", "Source", "Reference", "Collected (INR)", "Refunded (INR)", "Commission (INR)", "Net change (INR)", "Balance (INR)"]
SUMMARY = [("opening_balance", "Opening net balance"), ("collected", "Collections"), ("refunded", "Refunds"),
           ("commission", "Commission (net)"), ("net", "Net movement"), ("closing_balance", "Closing net balance")]
SOURCES = {"FOOD_ORDER": "Food order", "RESERVATION_FEE": "Reservation fee", "PREORDER": "Preorder"}


def format_date_range(start_date, end_date):
    return f"{start_date:%d/%m/%Y} to {end_date:%d/%m/%Y}"


def rows(report):
    for entry in report.items:
        yield [entry.occurred_at.astimezone(RESTAURANT_TIMEZONE), entry.kind.capitalize(),
            SOURCES.get(entry.source_type, entry.source_type), entry.reference,
            entry.collected, entry.refunded, entry.commission, entry.net, entry.balance]


def csv_text(value):
    if isinstance(value, datetime):
        return value.strftime("%d/%m/%Y %H:%M:%S")
    if isinstance(value, Decimal):
        return f"{value:.2f}"
    value = str(value)
    # Only untrusted text is escaped; negative numeric balances remain numeric.
    return "'" + value if re.match(r"^[\s\x00-\x1f]*[=+@-]", value) else value


def ledger_csv(report, *, title="DineBook Restaurant Ledger", entity_label="Restaurant", entity_name=None,
               summary=SUMMARY, headers=HEADERS, entry_rows=None):
    stream = io.StringIO(newline="")
    writer = csv.writer(stream)
    writer.writerow([title])
    writer.writerow([entity_label, csv_text(report.restaurant_name if entity_name is None else entity_name)])
    writer.writerow(["Period", format_date_range(report.start_date, report.end_date), "Timezone", report.timezone, "Currency", "INR"])
    writer.writerow(["Generated at (IST)", csv_text(report.as_of.astimezone(RESTAURANT_TIMEZONE))])
    writer.writerow([label for _, label in summary])
    writer.writerow([csv_text(getattr(report.summary, key)) for key, _ in summary])
    writer.writerow([])
    writer.writerow(headers)
    writer.writerows([csv_text(value) for value in row] for row in (rows(report) if entry_rows is None else entry_rows))
    return stream.getvalue().encode("utf-8-sig")


NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"


def tag(parent, element_name, **attributes):
    return SubElement(parent, f"{{{NS}}}{element_name}", {key: str(value) for key, value in attributes.items()})


def xml_text(value):
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", str(value))


def ledger_xlsx(report, *, title="DineBook Restaurant Ledger", entity_name=None, summary=SUMMARY,
                headers=HEADERS, entry_rows=None, widths=(24, 18, 20, 40, 20, 20, 22, 22, 22), sheet_name="Restaurant Ledger"):
    sheet = Element(f"{{{NS}}}worksheet")
    last = len(report.items) + 8
    last_column = chr(64 + max(len(headers), len(summary)))
    tag(sheet, "dimension", ref=f"A1:{last_column}{last}")
    views = tag(sheet, "sheetViews")
    view = tag(views, "sheetView", workbookViewId=0)
    tag(view, "pane", ySplit=8, topLeftCell="A9", activePane="bottomLeft", state="frozen")
    columns = tag(sheet, "cols")
    for index, width in enumerate(widths, 1):
        tag(columns, "col", min=index, max=index, width=width, customWidth=1)
    data = tag(sheet, "sheetData")

    def row(number, values, header=False):
        element = tag(data, "row", r=number, **({"ht": 32, "customHeight": 1} if header else {}))
        for index, value in enumerate(values):
            reference = f"{chr(65 + index)}{number}"
            if isinstance(value, datetime):
                local = value.replace(tzinfo=None)
                serial = Decimal(str((local - datetime(1899, 12, 30)).total_seconds())) / Decimal(86400)
                cell = tag(element, "c", r=reference, s=2)
                tag(cell, "v").text = str(serial)
            elif isinstance(value, (Decimal, int)):
                cell = tag(element, "c", r=reference, s=1)
                tag(cell, "v").text = str(value)
            else:
                cell = tag(element, "c", r=reference, t="inlineStr", s=3 if header else 0)
                tag(tag(cell, "is"), "t").text = xml_text(value)

    row(1, [title], header=True)
    row(2, [report.restaurant_name if entity_name is None else entity_name])
    row(3, [f"{format_date_range(report.start_date, report.end_date)} · {report.timezone} · INR"])
    row(4, [f"Generated {csv_text(report.as_of.astimezone(RESTAURANT_TIMEZONE))} IST"])
    row(5, [label for _, label in summary], header=True)
    row(6, [getattr(report.summary, key) for key, _ in summary])
    row(8, headers, header=True)
    for number, values in enumerate(rows(report) if entry_rows is None else entry_rows, 9):
        row(number, values)
    tag(sheet, "autoFilter", ref=f"A8:{chr(64 + len(headers))}{last}")
    merges = tag(sheet, "mergeCells", count=4)
    for number in range(1, 5):
        tag(merges, "mergeCell", ref=f"A{number}:{last_column}{number}")

    styles = Element(f"{{{NS}}}styleSheet")
    formats = tag(styles, "numFmts", count=1)
    tag(formats, "numFmt", numFmtId=165, formatCode="dd/mm/yyyy hh:mm:ss")
    fonts = tag(styles, "fonts", count=2)
    for bold in (False, True):
        font = tag(fonts, "font"); tag(font, "sz", val=11); tag(font, "name", val="Calibri")
        if bold:
            tag(font, "b"); tag(font, "color", rgb="FFFFFFFF")
    fills = tag(styles, "fills", count=3)
    for pattern in ("none", "gray125", "solid"):
        fill = tag(tag(fills, "fill"), "patternFill", patternType=pattern)
        if pattern == "solid": tag(fill, "fgColor", rgb="FF641B2B")
    border = tag(tag(styles, "borders", count=1), "border")
    for edge in ("left", "right", "top", "bottom", "diagonal"): tag(border, edge)
    tag(tag(styles, "cellStyleXfs", count=1), "xf", numFmtId=0, fontId=0, fillId=0, borderId=0)
    xfs = tag(styles, "cellXfs", count=4)
    for number_format, font, fill in ((0, 0, 0), (4, 0, 0), (165, 0, 0), (0, 1, 2)):
        style = tag(xfs, "xf", numFmtId=number_format, fontId=font, fillId=fill, borderId=0, xfId=0,
                    applyNumberFormat=1, applyFont=1, applyFill=1)
        if font:
            style.set("applyAlignment", "1")
            tag(style, "alignment", wrapText=1, vertical="center")
    tag(tag(styles, "cellStyles", count=1), "cellStyle", name="Normal", xfId=0, builtinId=0)
    output = io.BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", '''<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/><Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/></Types>''')
        archive.writestr("_rels/.rels", '''<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>''')
        workbook = Element(f"{{{NS}}}workbook")
        tag(tag(workbook, "sheets"), "sheet", name=sheet_name, sheetId=1,
            **{"{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id": "rId1"})
        archive.writestr("xl/workbook.xml", tostring(workbook, encoding="utf-8", xml_declaration=True))
        archive.writestr("xl/_rels/workbook.xml.rels", '''<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/><Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>''')
        archive.writestr("xl/styles.xml", tostring(styles, encoding="utf-8", xml_declaration=True))
        archive.writestr("xl/worksheets/sheet1.xml", tostring(sheet, encoding="utf-8", xml_declaration=True))
    return output.getvalue()


def pdf_text(value):
    text = str(value).replace("\n", " ").replace("\r", " ")
    return text.encode("cp1252", "replace").decode("cp1252").replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def ledger_pdf(report):
    entries = list(rows(report))
    pages = [entries[index:index + 20] for index in range(0, len(entries), 20)] or [[]]
    streams = []
    widths = [100, 110, 130, 90, 82, 82, 90, 90]
    headings = ["Date / time (IST)", "Source / entry", "Reference", "Collected", "Refunded", "Commission", "Net change", "Net balance"]
    for number, entries in enumerate(pages, 1):
        commands = []
        def text(x, y, value, size=8, color="0.16 0.14 0.14"):
            commands.append(f"{color} rg BT /F1 {size} Tf 1 0 0 1 {x} {y} Tm ({pdf_text(value)}) Tj ET")
        def line(y): commands.append(f"0.85 0.83 0.82 RG 0.5 w 32 {y} m 810 {y} l S")
        commands.append("0.39 0.11 0.17 rg 0 585 842 10 re f")
        text(32, 555, "DINEBOOK", 10, "0.39 0.11 0.17")
        text(32, 530, "Restaurant Ledger", 22)
        text(32, 509, report.restaurant_name[:110], 10)
        text(32, 493, f"{format_date_range(report.start_date, report.end_date)} | India Standard Time (IST) | All amounts INR", 8)
        text(32, 474, f"Opening: {report.summary.opening_balance:,.2f}   Collections: {report.summary.collected:,.2f}   Refunds: {report.summary.refunded:,.2f}   Commission (net): {report.summary.commission:,.2f}", 8)
        text(32, 459, f"Net movement: {report.summary.net:,.2f}   Closing: {report.summary.closing_balance:,.2f}   Total entries: {report.total}", 8)
        commands.append("0.96 0.94 0.93 rg 32 426 778 22 re f")
        x = 36
        for label, width in zip(headings, widths): text(x, 434, label, 7); x += width
        y = 414
        for entry in entries:
            values = [entry[0].strftime("%d/%m/%Y %H:%M"), f"{entry[2]} / {entry[1]}", entry[3], *[f"{value:,.2f}" for value in entry[4:]]]
            x = 36
            for index, (value, width) in enumerate(zip(values, widths)):
                size = 6 if index == 2 else 7
                if index >= 3:
                    text(x + width - 8 - len(value) * size * 0.53, y, value, size)
                else: text(x, y, value, size)
                x += width
            line(y - 6); y -= 18
        if not entries: text(36, 406, "No recorded collections or refunds in this period.", 10)
        line(38)
        text(32, 25, "Net collections after refunds and commission; separate from wallet balances.", 7)
        text(690, 25, f"Page {number} of {len(pages)}", 7)
        streams.append("\n".join(commands).encode("cp1252"))
    return pdf_document(streams)


def pdf_document(streams):
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{' '.join(f'{4 + i * 2} 0 R' for i in range(len(streams)))}] /Count {len(streams)} >>".encode(),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>"]
    for index, stream in enumerate(streams):
        objects.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 842 595] /Resources << /Font << /F1 3 0 R >> >> /Contents {5 + index * 2} 0 R >>".encode())
        objects.append(f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"\nendstream")
    result = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"); offsets = [0]
    for index, item in enumerate(objects, 1):
        offsets.append(len(result)); result.extend(f"{index} 0 obj\n".encode() + item + b"\nendobj\n")
    start = len(result)
    result.extend(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]: result.extend(f"{offset:010d} 00000 n \n".encode())
    result.extend(f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{start}\n%%EOF\n".encode())
    return bytes(result)


def export_ledger(report, format):
    if format == "csv": return ledger_csv(report), "text/csv; charset=utf-8"
    if format == "xlsx": return ledger_xlsx(report), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    return ledger_pdf(report), "application/pdf"
