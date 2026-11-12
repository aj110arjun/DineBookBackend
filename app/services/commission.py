"""Commission settlement, performed in the caller's payment transaction."""
import uuid
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP

from fastapi import HTTPException
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.models.revenue import PlatformCommission, PlatformCommissionPayout, PlatformRevenueSettings
from app.models.user import User, UserRole
from app.models.wallet import WalletAccount, WalletTransaction
from app.services.notifications import notify_wallet_transaction


CENT = Decimal("0.01")


def money(value) -> Decimal:
    return Decimal(value or 0).quantize(CENT, rounding=ROUND_HALF_UP)


def settings_row(db: Session) -> PlatformRevenueSettings:
    """Initialize and lock the shared rate, including on the first payment."""
    insert = sqlite_insert if db.get_bind().dialect.name == "sqlite" else pg_insert
    db.execute(insert(PlatformRevenueSettings).values(
        id=1, commission_percent=Decimal("10.00"), updated_at=datetime.now(timezone.utc),
    ).on_conflict_do_nothing(index_elements=["id"]))
    return db.query(PlatformRevenueSettings).filter(
        PlatformRevenueSettings.id == 1
    ).populate_existing().with_for_update().one()


def recipient_admin(db: Session) -> User | None:
    return db.query(User).filter(
        User.role == UserRole.ADMIN, User.is_active.is_(True)
    ).order_by(User.created_at.asc(), User.id.asc()).first()


def locked_wallets(db: Session, user_ids: list[uuid.UUID]) -> dict[uuid.UUID, WalletAccount]:
    db.flush()
    ids = sorted(set(user_ids), key=str)
    # Serialize creation of missing accounts as well as balance updates.
    db.query(User.id).filter(User.id.in_(ids)).order_by(User.id).with_for_update().all()
    accounts = {row.user_id: row for row in db.query(WalletAccount).filter(
        WalletAccount.user_id.in_(ids)
    ).order_by(WalletAccount.user_id).populate_existing().with_for_update().all()}
    for user_id in ids:
        if user_id not in accounts:
            accounts[user_id] = WalletAccount(user_id=user_id, balance=Decimal("0.00"))
            db.add(accounts[user_id])
    db.flush()
    return accounts


def record_wallet_entry(db: Session, account: WalletAccount, amount: Decimal,
                        description: str, reference: str) -> None:
    amount = money(amount)
    if amount == 0:
        return
    account.balance = money(account.balance) + amount
    transaction = WalletTransaction(user_id=account.user_id, amount=amount,
        transaction_type="CREDIT" if amount > 0 else "DEBIT",
        description=description, reference=reference)
    db.add(transaction)
    notify_wallet_transaction(db, transaction)


def settle_payment_commission(db: Session, record: PlatformCommission, *, require_balance: bool = True) -> None:
    """Settle a locked, snapshotted commission once; never commit here."""
    if record.payout_id is not None or record.refunded_at is not None:
        return
    admin = recipient_admin(db)
    if admin is None:
        raise HTTPException(status_code=503, detail="An active admin is required to settle commission.")
    accounts = locked_wallets(db, [record.manager_id, admin.id])
    amount = money(record.commission_amount)
    if require_balance and amount > 0 and money(accounts[record.manager_id].balance) < amount:
        raise HTTPException(status_code=409, detail=(
            f"Add money to the manager wallet before confirming this payment. "
            f"Commission required: {amount:.2f}."))
    payout = PlatformCommissionPayout(manager_id=record.manager_id, admin_id=admin.id, amount=amount)
    db.add(payout)
    db.flush()
    record.payout_id = payout.id
    reference = f"admin-commission-payout:{payout.id}"
    record_wallet_entry(db, accounts[record.manager_id], -amount, "Automatic admin commission", reference)
    record_wallet_entry(db, accounts[admin.id], amount, "Automatic restaurant commission", reference)
    db.flush()


def reverse_payment_commission(db: Session, record: PlatformCommission) -> None:
    """Return the original share to the original manager, at its original rate."""
    if record.refunded_at is not None:
        return
    if record.payout_id is not None:
        payout = db.query(PlatformCommissionPayout).filter(
            PlatformCommissionPayout.id == record.payout_id
        ).one()
        accounts = locked_wallets(db, [record.manager_id, payout.admin_id])
        amount = money(record.commission_amount)
        reference = f"admin-commission-refund:{record.id}"
        # Like existing restaurant refunds, reversals may take a wallet negative.
        # A customer refund must not depend on whether earnings were spent.
        record_wallet_entry(db, accounts[payout.admin_id], -amount,
            "Commission reversal for customer refund", reference)
        record_wallet_entry(db, accounts[record.manager_id], amount,
            "Commission returned for customer refund", reference)
    record.refunded_at = datetime.now(timezone.utc)
    db.flush()
