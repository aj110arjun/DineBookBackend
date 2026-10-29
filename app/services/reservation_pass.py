"""Render the downloadable reservation pass for confirmation emails."""
from io import BytesIO
from decimal import Decimal
import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
import qrcode

from app.services.reservation_invoice_pdf import _status


def _money(value) -> str:
    return f"INR {Decimal(str(value or 0)):.2f}"


def build_reservation_pass_png(item, restaurant, customer, tables, reservation_code) -> bytes:
    # Keep this payload compatible with the manager's pass scanner.
    qr = qrcode.QRCode(box_size=4, border=4)
    qr.add_data(json.dumps({"reservation_id": str(item.id)}, separators=(",", ":")))
    qr.make(fit=True)
    qr_image = qr.make_image(fill_color="black", back_color="white").convert("RGB")
    image = Image.new("RGB", (640, 860), "#f8f4ef")
    draw = ImageDraw.Draw(image)
    fonts = Path(__file__).resolve().parents[1] / "assets" / "fonts"

    def text(value, x, y, size=14, color="#382d2a", bold=False, anchor="ls", width=520):
        font = ImageFont.truetype(str(fonts / f"NotoSans-{'Bold' if bold else 'Regular'}.ttf"), size)
        value = str(value)
        while draw.textlength(value, font=font) > width and len(value) > 1:
            value = value[:-2].rstrip("…") + "…"
        draw.text((x, y), value, font=font, fill=color, anchor=anchor)

    draw.rounded_rectangle((28, 28, 612, 832), radius=20, fill="white")
    draw.rounded_rectangle((28, 28, 612, 140), radius=20, fill="#4f101d")
    draw.rectangle((28, 112, 612, 140), fill="#4f101d")
    text("DINEBOOK", 58, 72, 17, "#d7b96c", True)
    text("RESERVATION PASS", 58, 104, 12, "#d7b96c", True)
    text("CONFIRMED", 580, 78, 12, "#b4e5bf", True, "rs")
    text(customer.name or "Guest", 320, 184, 23, bold=True, anchor="ms")
    text(restaurant.name, 320, 211, 16, "#776b65", anchor="ms")
    # Integer module scaling preserves sharp QR edges and its quiet zone.
    image.paste(qr_image, ((640 - qr_image.width) // 2, 267 + (210 - qr_image.height) // 2))
    draw.line((60, 535, 580, 535), fill="#eee5e1")
    has_preorder = item.fulfillment_type == "PREORDER" and bool(item.preorder_items)
    rows = [
        ("Reservation code", reservation_code, True),
        ("Date and time", f"{item.reservation_date.isoformat()} · {item.start_time.strftime('%I:%M %p').lstrip('0')}", False),
        ("Guests", item.number_of_guests, False),
        ("Table", ", ".join(f"{table.floor.floor_name if table.floor else 'Dining area'} · {table.table_number}" for table in tables) or "Assignment pending", False),
        ("Reservation fee", _money(item.fee_amount), True),
        ("Reservation payment", _status(item.payment_method, item.payment_status, item.fee_amount), True),
        ("Preorder", _money(item.preorder_total) if has_preorder else "Not ordered", False),
        ("Preorder payment", _status(item.preorder_payment_method, item.preorder_payment_status, Decimal(str(item.preorder_total or 0))) if has_preorder else "Not ordered", True),
    ]
    for index, (label, value, bold) in enumerate(rows):
        y = 558 + index * 27
        text(label, 65, y, 13, "#938781", width=170)
        text(value, 575, y, bold=bold, anchor="rs", width=330)
    text("Present this pass when you arrive.", 320, 795, 11, "#938781", anchor="ms")
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()
