"""Visible bounded HTTP 429 waits; no retry inside the HTTP transport."""

import asyncio
from datetime import timezone
from email.utils import parsedate_to_datetime
import math
import time

from ..util import load_json, write_json


def is_rate_limit(error):
    return (isinstance(error, dict) and error.get("code") == "PROVIDER_HTTP_ERROR"
            and isinstance(error.get("details"), dict) and error["details"].get("http_status") == 429)


def retry_schedule(error, retry_number, observed_at):
    if not is_rate_limit(error) or type(retry_number) is not int or not 1 <= retry_number <= 3:
        raise ValueError("Invalid rate-limit retry")
    if type(observed_at) not in (int, float) or not math.isfinite(observed_at) or observed_at < 0:
        raise ValueError("Invalid rate-limit observation time")
    value = error["details"].get("response_headers", {}).get("retry-after")
    delay, basis = (30, 60, 120)[retry_number - 1], "bounded_backoff"
    if isinstance(value, str):
        try:
            if value.strip().isascii() and value.strip().isdigit():
                seconds = int(value.strip())
            else:
                stamp = parsedate_to_datetime(value)
                if stamp.tzinfo is None:
                    stamp = stamp.replace(tzinfo=timezone.utc)
                seconds = max(0, math.ceil(stamp.timestamp() - observed_at))
            if seconds > 600:
                return None
            delay, basis = seconds, "retry_after"
        except (ValueError, TypeError, OverflowError):
            pass
    return {"retry_number": retry_number, "observed_at": observed_at,
            "delay_seconds": delay, "basis": basis}


async def wait_for_retry(path, schedule, external):
    record = {"schedule": schedule, "mode": "elapsed" if external else "fixture_skipped", "completed": False}
    write_json(path, record)
    started = time.monotonic()
    if external:
        await asyncio.sleep(schedule["delay_seconds"])
    record.update(completed=True, elapsed_seconds=time.monotonic() - started)
    write_json(path, record)


def validate_wait(path, schedule, external):
    record = load_json(path)
    elapsed = record.get("elapsed_seconds")
    if (record.get("schedule") != schedule or record.get("completed") is not True
            or record.get("mode") != ("elapsed" if external else "fixture_skipped")
            or type(elapsed) not in (int, float) or not math.isfinite(elapsed) or elapsed < 0
            or external and elapsed + 0.01 < schedule["delay_seconds"]):
        raise ValueError("Rate-limit wait evidence differs from the declared retry")
