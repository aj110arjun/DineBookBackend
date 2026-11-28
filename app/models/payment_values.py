from enum import Enum


class PaymentStatus(str, Enum):
    PENDING = "PENDING"
    PAID = "PAID"
    FAILED = "FAILED"
    REFUNDED = "REFUNDED"
    CANCELLED = "CANCELLED"


class PaymentMethod(str, Enum):
    WALLET = "WALLET"
    RAZORPAY = "RAZORPAY"
    PAY_AT_DESK = "PAY_AT_DESK"


class PaymentSource(str, Enum):
    RESERVATION_FEE = "RESERVATION_FEE"
    FOOD_ORDER = "FOOD_ORDER"
    PREORDER = "PREORDER"


class TransactionFilterType(str, Enum):
    RESERVATION_FEE = "RESERVATION_FEE"
    FOOD_ORDER = "FOOD_ORDER"
    WALLET = "WALLET"


class RefundStatus(str, Enum):
    NONE = "NONE"
    REFUNDED = "REFUNDED"


class WalletEntryStatus(str, Enum):
    CREDITED = "CREDITED"
    DEBITED = "DEBITED"
    FAILED = "FAILED"
    REFUNDED = "REFUNDED"


class LedgerEntryType(str, Enum):
    COLLECTION = "COLLECTION"
    REFUND = "REFUND"
    SETTLEMENT = "SETTLEMENT"
