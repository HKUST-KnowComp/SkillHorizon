"""Standard-library-only cleanup for an explicitly owned process group."""

import os
import signal
import time


def terminate_group(process, *, deadline=None):
    errors, gone = [], False
    def budget(cap):
        return cap if deadline is None else max(0, min(cap, deadline - time.monotonic()))
    for sig, grace in ((signal.SIGTERM, 3), (signal.SIGKILL, 2)):
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            gone = True
            break
        except BaseException as error:
            errors.append(error)
            continue
        until = time.monotonic() + budget(grace)
        while time.monotonic() < until:
            try:
                process.poll()
                os.killpg(process.pid, 0)
            except ProcessLookupError:
                gone = True
                break
            except BaseException as error:
                errors.append(error)
                break
            time.sleep(min(.05, max(0, until - time.monotonic())))
        if gone:
            break
    try:
        process.wait(timeout=budget(10))
    except BaseException as error:
        errors.append(error)
    if not gone:
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            gone = True
        except BaseException as error:
            errors.append(error)
    for error in errors:
        if not isinstance(error, Exception):
            raise error
    if errors:
        raise errors[0]
    if not gone:
        raise RuntimeError("Process group quiescence unverified")
