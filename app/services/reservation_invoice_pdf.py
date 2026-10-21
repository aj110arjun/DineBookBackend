from __future__ import annotations

import textwrap
from decimal import Decimal

from app.models.dining import DiningTable
from app.models.reservation import Reservation
from app.models.restaurant import Restaurant
from app.models.user import User


def _pdf_text(value: object) -> str:
    return str(value).encode("latin-1", "replace").decode("latin-1").replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _money(value: Decimal | float | int) -> str:
    return f"INR {Decimal(str(value)):.2f}"


def build_reservation_invoice_pdf(
    item: Reservation,
    restaurant: Restaurant,
    customer: User,
    tables: list[DiningTable],
    reservation_code: str,
    preorder_amount: Decimal,
    preorder_discount: Decimal,
    preorder_subtotal: Decimal,
    paid: Decimal,
    due: Decimal,
    has_preorder: bool,
) -> bytes:
    pages: list[list[str]] = []

    def start_page(continued: bool = False) -> tuple[list[str], int]:
        commands: list[str] = []
        pages.append(commands)

        def text(x: int, y: int, size: int, value: object, color: str = "0.16 0.20 0.22") -> None:
            commands.append(f"{color} rg BT /F1 {size} Tf {x} {y} Td ({_pdf_text(value)}) Tj ET")

        commands.append("0.06 0.39 0.43 rg 0 822 595 20 re f")
        text(48, 790, 16, "DINEBOOK", "0.06 0.39 0.43")
        text(48, 758, 21, "Reservation invoice" + (" - continued" if continued else ""))
        text(48, 738, 8, "BOOKING REFERENCE", "0.40 0.44 0.46")
        text(48, 721, 12, reservation_code, "0.06 0.39 0.43")
        text(375, 780, 8, "INVOICE DATE", "0.40 0.44 0.46")
        text(375, 764, 9, item.created_at.strftime("%d %b %Y") if item.created_at else "—")
        text(375, 740, 8, "RESERVATION PAYMENT", "0.40 0.44 0.46")
        text(375, 724, 9, "PAID", "0.10 0.40 0.27")
        commands.append("0.06 0.39 0.43 RG 0.8 w 48 703 m 547 703 l S")
        if continued:
            text(48, 680, 8, "ITEMS - CONTINUED", "0.40 0.44 0.46")
            return commands, 650
        text(48, 682, 8, "BILLED BY", "0.40 0.44 0.46")
        text(48, 666, 10, "DineBook")
        text(310, 682, 8, "CUSTOMER", "0.40 0.44 0.46")
        text(310, 666, 10, customer.name[:38])
        if customer.email:
            text(310, 651, 8, customer.email[:48], "0.40 0.44 0.46")
        text(48, 632, 8, "RESTAURANT", "0.40 0.44 0.46")
        text(48, 616, 9, restaurant.name[:48])
        text(310, 632, 8, "RESERVATION DATE & TIME", "0.40 0.44 0.46")
        text(310, 616, 9, f"{item.reservation_date.strftime('%d %b %Y')} · {item.start_time.strftime('%I:%M %p')}")
        text(48, 596, 8, "PARTY SIZE", "0.40 0.44 0.46")
        text(48, 580, 9, f"{item.number_of_guests} guests · {len(tables)} table(s)")
        text(310, 596, 8, "BOOKED TABLES", "0.40 0.44 0.46")
        text(310, 580, 9, ", ".join(table.table_number for table in tables)[:42] or "Assignment pending")
        return commands, 548

    page, y = start_page()
    rows: list[tuple[str, int, Decimal, Decimal]] = []
    table_total = Decimal("0.00")
    for table in tables:
        table_fee = Decimal(table.reservation_fee or 0)
        table_total += table_fee
        floor = table.floor.floor_name if table.floor else "Dining area"
        rows.append((f"Table {table.table_number} · {table.capacity} seats · {floor}", 1, table_fee, table_fee))
    fee = Decimal(item.fee_amount or 0)
    if not rows:
        rows.append(("Reservation table fee", 1, fee, fee))
    elif table_total != fee:
        adjustment = fee - table_total
        rows.append(("Reservation fee adjustment", 1, adjustment, adjustment))
    if has_preorder:
        for entry in item.preorder_items or []:
            label = " ".join(str(part) for part in (entry.get("food_name"), entry.get("variant_name")) if part)
            quantity = int(entry.get("quantity", 1))
            unit_price = Decimal(str(entry.get("unit_price", 0)))
            amount = Decimal(str(entry.get("line_total", unit_price * quantity)))
            rows.append((f"Preorder · {label}", quantity, unit_price, amount))

    def table_header(target: list[str], top: int) -> None:
        target.append(f"0.93 0.96 0.96 rg 48 {top - 24} 499 25 re f")
        for x, label in ((57, "DESCRIPTION"), (368, "QTY"), (410, "UNIT PRICE"), (490, "AMOUNT")):
            target.append(f"0.40 0.44 0.46 rg BT /F1 8 Tf {x} {top - 14} Td ({label}) Tj ET")

    table_header(page, y)
    y -= 39
    for description, quantity, unit_price, amount in rows:
        wrapped = textwrap.wrap(description, width=46) or [description]
        height = max(28, len(wrapped) * 10 + 12)
        if y - height < 190:
            page, y = start_page(continued=True)
            table_header(page, y)
            y -= 39
        for line_number, line in enumerate(wrapped):
            page.append(f"0.16 0.20 0.22 rg BT /F1 8 Tf 57 {y - line_number * 10} Td ({_pdf_text(line)}) Tj ET")
        page.append(f"0.16 0.20 0.22 rg BT /F1 8 Tf 374 {y} Td ({quantity}) Tj ET")
        page.append(f"0.16 0.20 0.22 rg BT /F1 8 Tf 410 {y} Td ({_pdf_text(_money(unit_price))}) Tj ET")
        page.append(f"0.16 0.20 0.22 rg BT /F1 8 Tf 490 {y} Td ({_pdf_text(_money(amount))}) Tj ET")
        y -= height
        page.append(f"0.85 0.88 0.89 RG 0.5 w 48 {y + 5} m 547 {y + 5} l S")

    summary = 122 + (5 if has_preorder else 3) * 14
    if y - summary < 65:
        page, y = start_page(continued=True)
    y -= 12
    subtotal = fee + preorder_subtotal if has_preorder else fee
    total = fee + preorder_amount if has_preorder else fee
    totals: list[tuple[str, Decimal]] = [("Subtotal", subtotal)]
    if preorder_discount:
        totals.append((f"Offer discount · {item.preorder_offer_code or 'Offer'}", -preorder_discount))
    if has_preorder and Decimal(item.preorder_tax_amount or 0):
        totals.append((f"GST ({item.preorder_tax_percent}%)", Decimal(item.preorder_tax_amount)))
    totals.extend([("Total", total), ("Amount paid", paid), ("Amount due", due)])
    for label, value in totals:
        y -= 16
        page.append(f"0.40 0.44 0.46 rg BT /F1 8 Tf 330 {y} Td ({_pdf_text(label)}) Tj ET")
        page.append(f"0.16 0.20 0.22 rg BT /F1 8 Tf 468 {y} Td ({_pdf_text(_money(value))}) Tj ET")
    y -= 25
    page.append(f"0.06 0.39 0.43 rg BT /F1 9 Tf 48 {y} Td (PAYMENT DETAILS) Tj ET")
    payment_rows = [
        ("Reservation fee", f"{_status(item.payment_method, item.payment_status, fee)} · {_money(fee)}"),
        ("Preorder", "Not ordered" if not has_preorder else _status(item.preorder_payment_method, item.preorder_payment_status, preorder_amount)),
    ]
    if has_preorder and item.preorder_offer_code:
        payment_rows.append(("Offer applied", item.preorder_offer_code))
    for label, value in payment_rows:
        y -= 14
        page.append(f"0.40 0.44 0.46 rg BT /F1 8 Tf 48 {y} Td ({_pdf_text(label)}) Tj ET")
        page.append(f"0.16 0.20 0.22 rg BT /F1 8 Tf 200 {y} Td ({_pdf_text(value[:58])}) Tj ET")

    for page_number, commands in enumerate(pages, start=1):
        commands.append("0.83 0.86 0.87 RG 0.7 w 48 53 m 547 53 l S")
        commands.append("0.40 0.44 0.46 rg BT /F1 7 Tf 48 38 Td (DineBook reservation invoice - keep for your records) Tj ET")
        commands.append(f"0.40 0.44 0.46 rg BT /F1 7 Tf 500 38 Td (Page {page_number}/{len(pages)}) Tj ET")
    streams = ["\n".join(commands).encode("latin-1") for commands in pages]
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{' '.join(f'{4 + i * 2} 0 R' for i in range(len(pages)))}] /Count {len(pages)} >>".encode(),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    for index, stream in enumerate(streams):
        page_object = 4 + index * 2
        objects.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 3 0 R >> >> /Contents {page_object + 1} 0 R >>".encode())
        objects.append(b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream")
    output = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, obj in enumerate(objects, start=1):
        offsets.append(len(output))
        output.extend(f"{index} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref_offset = len(output)
    output.extend(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        output.extend(f"{offset:010d} 00000 n \n".encode())
    output.extend(f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF".encode())
    return bytes(output)


def _status(method: str | None, status: str | None, amount: Decimal) -> str:
    if status == "PAID":
        return "Paid via " + {"WALLET": "DineBook wallet", "RAZORPAY": "Razorpay", "PAY_AT_DESK": "restaurant"}.get(method or "", "payment method")
    if method == "PAY_AT_DESK":
        return f"Pay at restaurant · {_money(amount)} due"
    return f"{(status or 'PENDING').replace('_', ' ').title()} · {_money(amount)} due"
