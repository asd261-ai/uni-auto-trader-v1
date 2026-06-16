"""Run a blocking call with a hard timeout, in a throwaway daemon thread.

The Unitrade broker SDK makes synchronous C-native calls with no timeout; a hung
internal socket blocks the caller forever and wedges the whole poll loop.
call_with_timeout bounds that: it runs fn in a FRESH daemon thread and waits up
to `timeout`. On timeout it raises SDKCallTimeout and ABANDONS the thread (daemon
→ dies with the process). A fresh thread per call (not a shared pool) means a
stuck call never blocks the next one. A truly wedged SDK is then caught by the
poll-loop liveness watchdog. See spec 2026-06-17, component 3.
"""
from __future__ import annotations

import threading
from typing import Any, Callable


class SDKCallTimeout(Exception):
    pass


def call_with_timeout(fn: Callable[..., Any], *args: Any, timeout: float, **kwargs: Any) -> Any:
    result: list = []
    error: list = []

    def _run() -> None:
        try:
            result.append(fn(*args, **kwargs))
        except BaseException as e:        # surface the SDK's real error to the caller
            error.append(e)

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        raise SDKCallTimeout(f"call to {getattr(fn, '__name__', fn)!r} exceeded {timeout}s")
    if error:
        raise error[0]
    return result[0] if result else None
