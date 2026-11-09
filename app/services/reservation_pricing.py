from collections.abc import Iterable
from decimal import Decimal

from app.models.dining import DiningTable


def reservation_fee_quote(tables: Iterable[DiningTable], duration_minutes: int) -> dict:
    """Price the first hour plus each started additional hour for the selected tables."""
    tables = tuple(tables)
    cent = Decimal("0.01")
    base = sum((Decimal(str(table.reservation_fee or 0)) for table in tables), Decimal("0.00"))
    rate = sum((Decimal(str(table.extra_hour_fee or 0)) for table in tables), Decimal("0.00"))
    extra_hours = max(0, (duration_minutes - 60 + 59) // 60)
    extra = (rate * extra_hours).quantize(cent)
    return {
        "base_fee_amount": base.quantize(cent),
        "extra_hour_rate": rate.quantize(cent),
        "extra_hours": extra_hours,
        "extra_fee_amount": extra,
        "fee_amount": (base + extra).quantize(cent),
    }
