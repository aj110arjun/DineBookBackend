from decimal import Decimal, ROUND_HALF_UP
import uuid

from sqlalchemy.orm import Session

from app.models.restaurant import Restaurant
from app.models.user import User
from app.models.wallet import WalletAccount, WalletTransaction
from app.services.notifications import notify_wallet_transaction


CENT = Decimal("0.01")


def record_restaurant_payment(db: Session, restaurant_id: uuid.UUID, amount: Decimal,
                              description: str, reference: str) -> None:
    """Credit a customer payment to the restaurant manager's wallet and ledger."""
    amount = Decimal(amount).quantize(CENT, rounding=ROUND_HALF_UP)
    if amount <= 0:
        return
    restaurant = db.query(Restaurant).filter(Restaurant.id == restaurant_id).with_for_update().first()
    if restaurant is None or restaurant.manager_id is None:
        raise ValueError("A restaurant manager is required to credit a restaurant payment.")

    # Serialize wallet creation for managers whose account does not exist yet.
    db.query(User.id).filter(User.id == restaurant.manager_id).with_for_update().first()
    account = db.query(WalletAccount).filter(WalletAccount.user_id == restaurant.manager_id).with_for_update().first()
    if account is None:
        account = WalletAccount(user_id=restaurant.manager_id, balance=Decimal("0.00"))
        db.add(account)
        db.flush()
    account.balance = Decimal(account.balance) + amount
    transaction = WalletTransaction(user_id=restaurant.manager_id, amount=amount, transaction_type="CREDIT",
        description=description, reference=reference)
    db.add(transaction)
    notify_wallet_transaction(db, transaction)


def reverse_restaurant_payment(db: Session, restaurant_id: uuid.UUID, amount: Decimal,
                               description: str, reference: str) -> None:
    """Record a wallet refund as a debit from the restaurant wallet."""
    amount = Decimal(amount).quantize(CENT, rounding=ROUND_HALF_UP)
    if amount <= 0:
        return
    restaurant = db.query(Restaurant).filter(Restaurant.id == restaurant_id).with_for_update().first()
    if restaurant is None or restaurant.manager_id is None:
        raise ValueError("A restaurant manager is required to reverse a restaurant payment.")
    db.query(User.id).filter(User.id == restaurant.manager_id).with_for_update().first()
    account = db.query(WalletAccount).filter(WalletAccount.user_id == restaurant.manager_id).with_for_update().first()
    if account is None:
        account = WalletAccount(user_id=restaurant.manager_id, balance=Decimal("0.00"))
        db.add(account)
        db.flush()
    account.balance = Decimal(account.balance) - amount
    transaction = WalletTransaction(user_id=restaurant.manager_id, amount=-amount, transaction_type="DEBIT",
        description=description, reference=reference)
    db.add(transaction)
    notify_wallet_transaction(db, transaction)
