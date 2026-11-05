"""One-time historical commission reconciliation; preview unless --apply is supplied."""
import argparse
import json
from decimal import Decimal

from app.db.database import SessionLocal
from app.models.orders import FoodOrder
from app.models.reservation import Reservation
from app.models.revenue import PlatformCommission
from app.routers.commerce.revenue import sync_commission_records
from app.services.commission import money, reverse_payment_commission, settings_row, settle_payment_commission
from app.services.restaurant_wallet import locked_restaurant


def source_refunded(db, record):
    if record.source_type == "FOOD_ORDER":
        row = db.get(FoodOrder, record.source_id)
        return row is not None and row.payment_status == "REFUNDED"
    row = db.get(Reservation, record.source_id)
    if row is None:
        return False
    status = row.payment_status if record.source_type == "RESERVATION_FEE" else row.preorder_payment_status
    return status == "REFUNDED"


def reconcile(db, *, apply=False, backfill_rate=None):
    """Do not credit gross historical payments a second time."""
    settings = settings_row(db)
    rate = money(backfill_rate if backfill_rate is not None else settings.commission_percent)
    if not Decimal("0") <= rate <= Decimal("100"):
        raise ValueError("Backfill rate must be between 0 and 100.")
    count_before = db.query(PlatformCommission).count()
    sync_commission_records(db, None, rate)
    db.flush()
    backfilled = db.query(PlatformCommission).count() - count_before
    rows = db.query(PlatformCommission).filter(
        PlatformCommission.refunded_at.is_(None)
    ).order_by(PlatformCommission.restaurant_id, PlatformCommission.id).with_for_update().all()
    refunded = [row for row in rows if source_refunded(db, row)]
    refunded_ids = {row.id for row in refunded}
    pending = [row for row in rows if row.payout_id is None and row.id not in refunded_ids]
    result = {
        "applied": apply, "backfilled_records": backfilled,
        "backfill_percent": str(rate), "pending_records": len(pending),
        "commission_to_transfer": str(money(sum((row.commission_amount for row in pending), Decimal("0")))),
        "refunded_records_to_reconcile": len(refunded),
    }
    if not apply:
        db.rollback()
        return result
    if backfilled and backfill_rate is None:
        raise ValueError("Missing historical rates: review the preview and supply --backfill-rate before --apply.")
    for row in refunded:
        locked_restaurant(db, row.restaurant_id)
        # Old refunds already debited the manager's gross amount. Return only
        # any previously settled commission, without repeating that gross debit.
        reverse_payment_commission(db, row)
    for row in pending:
        locked_restaurant(db, row.restaurant_id)
        settle_payment_commission(db, row)
    db.commit()
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Apply the previewed wallet transfers atomically")
    parser.add_argument("--backfill-rate", type=Decimal, help="Historical percent for payments without a commission snapshot")
    args = parser.parse_args()
    with SessionLocal() as db:
        try:
            result = reconcile(db, apply=args.apply, backfill_rate=args.backfill_rate)
        except Exception:
            db.rollback()
            raise
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
