"""Customer draws, records and REF section 7 rows (BIZ-01).

The draw table for a new customer, in this order and the same count whatever happens:

- customers stream: country `choice_index` over the country weights, then city `pick` over
  that country's cities (an arrival's gap is drawn by the engine before these);
- text stream: first-name `pick`, then last-name `pick`.

A record holds indices and ints, never text; `row` builds the text from the word lists and the
country table, so an index never outlives the lists that give it meaning.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from .. import clock, countries, textgen
from ..rng import Stream, StreamName
from ..state import CustomerRec


@dataclass(frozen=True)
class CustomerDraws:
    country_idx: int
    city_idx: int
    first_idx: int
    last_idx: int


def _index(value: int | None) -> int:
    """A `pick` over a non-empty list is never None; an empty list is a reference-data bug."""
    if value is None:
        raise countries.ReferenceDataError("a word list or city list is empty")
    return value


def draw_new_customer(streams: Mapping[StreamName, Stream]) -> CustomerDraws:
    """The four draws a new customer takes, in the table's order."""
    table = countries.countries()
    customers = streams[StreamName.CUSTOMERS]
    text = streams[StreamName.TEXT]
    country_idx = customers.choice_index(countries.country_weights())
    city_idx = _index(customers.pick(len(table[country_idx].cities)))
    first_idx = _index(text.pick(len(textgen.words("first_names"))))
    last_idx = _index(text.pick(len(textgen.words("last_names"))))
    return CustomerDraws(country_idx, city_idx, first_idx, last_idx)


def new_record(customer_id: int, ts_us: int, draws: CustomerDraws) -> CustomerRec:
    """A fresh customer: the email starts from the name's indices, version 0, not deleted."""
    return CustomerRec(
        customer_id=customer_id,
        created_us=ts_us,
        deleted_us=None,
        first_idx=draws.first_idx,
        last_idx=draws.last_idx,
        email_first_idx=draws.first_idx,
        email_last_idx=draws.last_idx,
        email_version=0,
        country_idx=draws.country_idx,
        city_idx=draws.city_idx,
    )


def row(rec: CustomerRec, updated_us: int) -> dict[str, object]:
    """The eight `customers` columns; `updated_us` is the tick that writes the row."""
    country = countries.countries()[rec.country_idx]
    return {
        "customer_id": rec.customer_id,
        "email": textgen.email(
            rec.email_first_idx, rec.email_last_idx, rec.customer_id, rec.email_version
        ),
        "full_name": textgen.full_name(rec.first_idx, rec.last_idx),
        "city": country.cities[rec.city_idx],
        "country": country.code,
        "deleted_at": None if rec.deleted_us is None else clock.to_datetime(rec.deleted_us),
        "created_at": clock.to_datetime(rec.created_us),
        "updated_at": clock.to_datetime(updated_us),
    }
