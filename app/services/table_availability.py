"""Date-scoped seating availability shared by booking and floor previews."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.models.reservation import Reservation, ReservationTable

RESTAURANT_TIMEZONE = ZoneInfo("Asia/Kolkata")
BLOCKING = ("PENDING_PAYMENT", "CONFIRMED", "SEATED")
RESERVATION_DURATION = timedelta(minutes=90)


def table_statuses(db, tables, day, start, end):
    """Return computed statuses without changing a table's operational state."""
    begins = datetime.combine(day, start)
    finishes = datetime.combine(day, end)
    if finishes <= begins:
        finishes += timedelta(days=1)
    table_ids = [table.id for table in tables]
    blocked = {}
    if table_ids:
        bookings = db.query(ReservationTable.table_id, Reservation).join(
            Reservation, Reservation.id == ReservationTable.reservation_id
        ).filter(
            ReservationTable.table_id.in_(table_ids),
            Reservation.status.in_(BLOCKING),
            Reservation.reservation_date >= day - timedelta(days=1),
            Reservation.reservation_date <= finishes.date(),
        ).all()
        for table_id, booking in bookings:
            booking_start = datetime.combine(booking.reservation_date, booking.start_time)
            booking_end = datetime.combine(booking.reservation_date, booking.end_time)
            if booking_end <= booking_start:
                booking_end += timedelta(days=1)
            if booking_start < finishes and booking_end > begins:
                status = "occupied" if booking.status == "SEATED" else "reserved"
                if blocked.get(table_id) != "occupied":
                    blocked[table_id] = status
    result = {}
    for table in tables:
        status = table.status
        # Manual occupancy/reservation applies to the local date it was set.
        # Future bookings are represented by Reservation rows, never this flag.
        if status in ("occupied", "reserved"):
            changed = table.updated_at
            if changed is not None:
                if changed.tzinfo is None:
                    from datetime import timezone
                    changed = changed.replace(tzinfo=timezone.utc)
                changed_day = changed.astimezone(RESTAURANT_TIMEZONE).date()
            else:
                changed_day = datetime.now(RESTAURANT_TIMEZONE).date()
            if day != changed_day:
                status = "available"
        result[table.id] = blocked.get(table.id, status) if status == "available" else status
    return result
