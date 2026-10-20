"""Exact synthetic USD ledger arithmetic, with no source float accounting."""

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from fractions import Fraction


def cents(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError("Amount must be finite USD with at most two decimal places")
    try:
        decimal = Decimal(str(value))
    except InvalidOperation:
        raise ValueError("Invalid USD amount") from None
    if not decimal.is_finite() or decimal < 0 or decimal * 100 != (decimal * 100).to_integral_value():
        raise ValueError("Amount must be nonnegative finite USD with at most two decimal places")
    return int(decimal * 100)


def instant(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("Explicit timezone required")
    return result.astimezone(timezone.utc)


def hours(start, end):
    delta = instant(end) - instant(start)
    microseconds = (delta.days * 86400 + delta.seconds) * 1000000 + delta.microseconds
    if microseconds < 0:
        raise ValueError("Reversed interval")
    return Fraction(microseconds, 3600000000)


def rounded(value):
    value = Fraction(value)
    if value < 0:
        raise ValueError("Negative ledger value")
    return (2 * value.numerator + value.denominator) // (2 * value.denominator)


def apportion(total, weights):
    if type(total) is not int or total < 0 or not weights or any(Fraction(v) < 0 for v in weights.values()):
        raise ValueError("Invalid apportionment")
    denominator = sum(Fraction(v) for v in weights.values())
    if denominator == 0:
        if total:
            raise ValueError("Cannot allocate nonzero amount to zero weights")
        return {key: 0 for key in weights}
    exact = {key: Fraction(total) * Fraction(value) / denominator for key, value in weights.items()}
    result = {key: value.numerator // value.denominator for key, value in exact.items()}
    remaining = total - sum(result.values())
    order = sorted(exact, key=lambda key: (-(exact[key] - result[key]), key))
    for key in order[:remaining]:
        result[key] += 1
    return result


def commitment_schedule(hourly_cents, start, end, periods):
    if type(hourly_cents) is not int or hourly_cents <= 0 or instant(end) <= instant(start):
        raise ValueError("Invalid commitment")
    ordered = sorted(periods, key=lambda p: instant(p["start"]))
    if len({p["period_id"] for p in ordered}) != len(ordered):
        raise ValueError("Duplicate period")
    for i, period in enumerate(ordered):
        if instant(period["end"]) <= instant(period["start"]):
            raise ValueError("Invalid billing period")
        if i and instant(ordered[i - 1]["end"]) != instant(period["start"]):
            raise ValueError("Periods must be contiguous and nonoverlapping")
    portions = {}
    covered = Fraction(0)
    for period in ordered:
        left, right = max(instant(start), instant(period["start"])), min(instant(end), instant(period["end"]))
        duration = hours(left.isoformat(), right.isoformat()) if right > left else Fraction(0)
        portions[period["period_id"]] = duration * hourly_cents
        covered += duration
    if covered != hours(start, end):
        raise ValueError("Commitment extends outside declared billing horizon")
    total = rounded(covered * hourly_cents)
    return total, apportion(total, portions)
