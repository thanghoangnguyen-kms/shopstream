"""Payment rows: a capture at payment, a refund after delivery (BIZ-05, BIZ-08).

`payments` is insert-only. A payment's currency is always its order's and its method is the one
drawn at placement, so a refund repeats its capture's method. `paid_at` equals the tick that
inserts the row (ADR-004 C4), and so do `created_at` and `updated_at`.
"""

from __future__ import annotations

from .. import clock, money
from ..config import PAYMENT_METHODS
from ..state import OrderRec
from .orders import CURRENCIES

CAPTURE = "capture"
REFUND = "refund"


def payment_row(
    payment_id: int, order: OrderRec, kind: str, amount_cents: int, ts_us: int
) -> dict[str, object]:
    """The `payments` columns of one capture or refund of `amount_cents` in the order's currency."""
    stamp = clock.to_datetime(ts_us)
    return {
        "payment_id": payment_id,
        "order_id": order.order_id,
        "payment_kind": kind,
        "amount": money.cents_to_decimal(amount_cents),
        "currency_code": CURRENCIES[order.currency_idx],
        "payment_method": PAYMENT_METHODS[order.method_idx],
        "paid_at": stamp,
        "created_at": stamp,
        "updated_at": stamp,
    }
