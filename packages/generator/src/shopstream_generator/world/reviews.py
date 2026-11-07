"""Review rows and the product a review names (BIZ-06, BIZ-08, D-16).

A review is written after delivery for the product of one line that survived the order's line
delete, by the order's customer, and only while that customer isn't soft-deleted. Moderation
removes it later as an update then a delete in one tick. The body is composed from the three
review word lists by the indices drawn at placement; the planted injection review is Phase 3's.
"""

from __future__ import annotations

from .. import clock, textgen
from ..state import OrderRec


def reviewed_product_id(order: OrderRec) -> int:
    """The product of the surviving line the placement draw chose.

    The draw is an index into the lines that were not deleted, in line order, and the line
    delete falls before payment, so the surviving set is final by the time a review is due.
    """
    surviving = [
        product
        for product, deleted in zip(order.line_products, order.line_deleted, strict=True)
        if not deleted
    ]
    return surviving[order.review_line]


def review_row(
    review_id: int, order: OrderRec, product_id: int, ts_us: int, updated_us: int
) -> dict[str, object]:
    """The `reviews` columns; `ts_us` is the insert tick and `updated_us` the tick writing the row."""
    return {
        "review_id": review_id,
        "product_id": product_id,
        "customer_id": order.customer_id,
        "rating": order.rating,
        "body": textgen.review_body(order.opener_idx, order.detail_idx, order.closer_idx),
        "created_at": clock.to_datetime(ts_us),
        "updated_at": clock.to_datetime(updated_us),
    }
