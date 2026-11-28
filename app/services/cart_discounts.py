"""Immutable item-level discount snapshots shared by checkout and fulfillment."""
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_DOWN

CENT = Decimal("0.01")


def offer_snapshot(offer) -> dict:
    return {"id": str(offer.id), "code": offer.code, "title": offer.title,
            "application_mode": offer.application_mode, "discount_type": offer.discount_type,
            "discount_percent": float(offer.discount_percent)}


def allocate_discount(discount: Decimal, subtotals: dict) -> dict:
    """Split a single offer's saving without multiplying its cap or fixed value."""
    total = sum(subtotals.values(), Decimal("0.00"))
    if total <= 0 or discount <= 0:
        return {}
    exact = {key: discount * amount / total for key, amount in subtotals.items()}
    allocated = {key: value.quantize(CENT, rounding=ROUND_DOWN) for key, value in exact.items()}
    remaining = int((discount - sum(allocated.values(), Decimal("0.00"))) / CENT)
    for key in sorted(exact, key=lambda key: (-(exact[key] - allocated[key]), str(key)))[:remaining]:
        allocated[key] += CENT
    return allocated


def applied_offer_totals(items: list, legacy_id=None, legacy_code=None, legacy_discount=0) -> list[dict]:
    grouped = {}
    for item in items or []:
        offer = item.get("applied_offer")
        amount = Decimal(str(item.get("discount_amount", 0)))
        if offer and amount > 0:
            entry = grouped.setdefault(offer["id"], {**offer, "discount_amount": Decimal("0.00")})
            entry["discount_amount"] += amount
    if not grouped and legacy_id and Decimal(legacy_discount or 0) > 0:
        grouped[str(legacy_id)] = {"id": str(legacy_id), "code": legacy_code,
                                   "discount_amount": Decimal(legacy_discount)}
    return [{**value, "discount_amount": float(value["discount_amount"])} for _, value in sorted(grouped.items())]


@dataclass
class CartOfferPricing:
    line_offers: dict = field(default_factory=dict)
    eligible_subtotal: Decimal = Decimal("0.00")
    reason: str | None = None
    checked_offer: object | None = None

    @property
    def offers(self):
        unique = {str(offer.id): offer for offer, _ in self.line_offers.values()}
        return [unique[key] for key in sorted(unique)]

    @property
    def discount_amount(self):
        return sum((amount for _, amount in self.line_offers.values()), Decimal("0.00"))

    @property
    def applied_offers(self):
        return applied_offer_totals(list(self.item_discounts.values()))

    @property
    def offer(self):
        if self.checked_offer is not None:
            return self.checked_offer
        totals = {item["id"]: item["discount_amount"] for item in self.applied_offers}
        return max(self.offers, key=lambda offer: totals[str(offer.id)], default=None)

    @property
    def offer_id(self):
        return self.offers[0].id if len(self.offers) == 1 else None

    @property
    def offer_code(self):
        return self.offers[0].code if len(self.offers) == 1 else "Automatic offers" if self.offers else None

    @property
    def item_discounts(self):
        return {str(key): {"applied_offer": offer_snapshot(offer), "discount_amount": float(amount)}
                for key, (offer, amount) in self.line_offers.items()}

    def apply(self, items: list) -> list:
        discounts = self.item_discounts
        return [{**item, **discounts[item["variant_id"]],
                 "discounted_line_total": float(Decimal(str(item["line_total"])) - Decimal(str(discounts[item["variant_id"]]["discount_amount"])))}
                if item["variant_id"] in discounts else dict(item) for item in items]
