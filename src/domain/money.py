"""Decimal money and quantity helpers. No floats anywhere in business arithmetic."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

PAISE = Decimal("0.01")
ZERO = Decimal("0")


def to_decimal(value: object) -> Decimal | None:
    """Parse a Decimal from str/int/Decimal. Floats are rejected to avoid binary rounding."""
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        raise TypeError("float is not allowed for money or quantity; pass a string")
    try:
        return Decimal(str(value).replace(",", "").strip())
    except InvalidOperation as exc:
        raise ValueError(f"not a decimal: {value!r}") from exc


def money(value: Decimal) -> Decimal:
    """Round to paise using ROUND_HALF_UP (SPEC.md §6)."""
    return value.quantize(PAISE, rounding=ROUND_HALF_UP)


def to_paise(value: Decimal) -> int:
    return int(money(value) * 100)


def fmt_qty(value: Decimal | None) -> str:
    if value is None:
        return "unknown"
    normalized = value.normalize()
    text = f"{normalized:f}"
    return text


def fmt_inr(value: Decimal | None) -> str:
    """Format INR with Indian digit grouping, e.g. ₹1,41,600.00."""
    if value is None:
        return "unknown"
    amount = money(value)
    sign = "-" if amount < 0 else ""
    whole, frac = f"{abs(amount):.2f}".split(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        whole = ",".join(groups + [tail])
    return f"{sign}₹{whole}.{frac}"
