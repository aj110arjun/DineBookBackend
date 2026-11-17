"""Platform ledger downloads; balances are outstanding commission receivables."""
from textwrap import wrap

from app.services.manager_ledger_exports import SOURCES, format_date_range, ledger_csv, ledger_xlsx, pdf_document, pdf_text
from app.services.table_availability import RESTAURANT_TIMEZONE

HEADERS = ["Date / time (IST)", "Restaurant", "Entry", "Source", "Reference", "Gross payments (INR)",
           "Payment refunds (INR)", "Commission earned (INR)", "Commission settled, net (INR)",
           "Receivable change (INR)", "Outstanding commission (INR)"]
SUMMARY = [("opening_balance", "Opening outstanding commission"), ("gross", "Gross payments"),
           ("refunds", "Payment refunds"), ("generated", "Commission generated"), ("reversed", "Commission reversed"),
           ("earned", "Commission earned"), ("collected", "Commission settlements"),
           ("collection_reversals", "Settlement reversals"), ("collected_net", "Commission settled, net"),
           ("net", "Receivable movement"), ("closing_balance", "Closing outstanding commission")]


def source_name(source):
    return "Commission settlement" if source == "COMMISSION_SETTLEMENT" else SOURCES.get(source, source)


def rows(report):
    for entry in report.items:
        yield [entry.occurred_at.astimezone(RESTAURANT_TIMEZONE), entry.restaurant_name, entry.kind.capitalize(),
               source_name(entry.source_type), entry.reference, entry.gross, entry.refunds, entry.earned,
               entry.collected_net, entry.net, entry.balance]


def ledger_pdf(report):
    pages = [report.items[index:index + 8] for index in range(0, len(report.items), 8)] or [[]]
    streams = []
    widths = [94, 116, 135, 72, 72, 72, 72, 72, 72]
    headings = ["Date / time (IST)", "Source / entry", "Reference", "Gross", "Refunds", "Earned", "Settled, net", "Change", "Outstanding"]
    for number, entries in enumerate(pages, 1):
        commands = []

        def text(x, y, value, size=8, color="0.16 0.14 0.14"):
            commands.append(f"{color} rg BT /F1 {size} Tf 1 0 0 1 {x} {y} Tm ({pdf_text(value)}) Tj ET")

        def line(y):
            commands.append(f"0.85 0.83 0.82 RG 0.5 w 32 {y} m 810 {y} l S")

        commands.append("0.39 0.11 0.17 rg 0 585 842 10 re f")
        text(32, 555, "DINEBOOK", 10, "0.39 0.11 0.17")
        text(32, 530, "Platform Ledger", 22)
        text(32, 509, "All DineBook restaurants - payments, refunds and commission settlements", 10)
        text(32, 493, f"{format_date_range(report.start_date, report.end_date)} | India Standard Time (IST) | All amounts INR")
        summary = report.summary
        text(32, 475, f"Opening outstanding: {summary.opening_balance:,.2f}   Gross payments: {summary.gross:,.2f}   Payment refunds: {summary.refunds:,.2f}")
        text(32, 460, f"Commission generated: {summary.generated:,.2f}   Reversed: {summary.reversed:,.2f}   Earned: {summary.earned:,.2f}")
        text(32, 445, f"Settlements: {summary.collected:,.2f}   Settlement reversals: {summary.collection_reversals:,.2f}   Settled, net: {summary.collected_net:,.2f}")
        text(32, 430, f"Receivable movement: {summary.net:,.2f}   Closing outstanding: {summary.closing_balance:,.2f}   Entries: {report.total}")
        commands.append("0.96 0.94 0.93 rg 32 399 778 22 re f")
        x = 36
        for label, width in zip(headings, widths):
            text(x, 407, label, 7)
            x += width
        y = 386
        for entry in entries:
            description = "Commission settlement" if entry.kind == "SETTLEMENT" else f"{source_name(entry.source_type)} / {entry.kind.capitalize()}"
            values = [entry.occurred_at.astimezone(RESTAURANT_TIMEZONE).strftime("%d/%m/%Y %H:%M"), description,
                      entry.reference, *[f"{value:,.2f}" for value in (entry.gross, entry.refunds, entry.earned, entry.collected_net, entry.net, entry.balance)]]
            x = 36
            for index, (value, width) in enumerate(zip(values, widths)):
                size = min(7, (width - 9) / (max(1, len(value)) * 0.6))
                if index >= 3:
                    text(x + width - 8 - len(value) * size * 0.53, y, value, size)
                else:
                    text(x, y, value, size)
                x += width
            for offset, label in enumerate(wrap(f"Restaurant: {entry.restaurant_name}", width=160)):
                text(36, y - 12 - offset * 9, label, 7, "0.42 0.39 0.39")
            line(y - 28)
            y -= 40
        if not entries:
            text(36, 382, "No recorded platform ledger entries in this period.", 10)
        line(38)
        text(32, 25, "Outstanding = commission earned less net internal settlements. Separate from wallet balances and bank receipts.", 7)
        text(710, 25, f"Page {number} of {len(pages)}", 7)
        streams.append("\n".join(commands).encode("cp1252"))
    return pdf_document(streams)


def export_ledger(report, format):
    options = dict(title="DineBook Platform Ledger", entity_name="All DineBook restaurants",
                   summary=SUMMARY, headers=HEADERS, entry_rows=rows(report))
    if format == "csv":
        return ledger_csv(report, entity_label="Scope", **options), "text/csv; charset=utf-8"
    if format == "xlsx":
        return ledger_xlsx(report, widths=(24, 36, 18, 26, 40, 24, 24, 25, 29, 28, 30),
                           sheet_name="Platform Ledger", **options), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    return ledger_pdf(report), "application/pdf"
