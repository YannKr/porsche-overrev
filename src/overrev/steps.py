"""Progress for `overrev read`: one line per step with a tick or cross, and a plain next step when one fails."""
from __future__ import annotations

import sys
from contextlib import contextmanager

from .transport import TransportError
from .uds import NegativeResponse

ADAPTER, CAR, READ = "adapter", "car", "read"

TEXT = {
    ADAPTER: ("Connected to the adapter", "Could not connect to the adapter"),
    CAR: ("The car answers", "The car does not answer"),
    READ: ("Read the over-rev counters", "Could not read the over-rev counters"),
}

HINT_ADAPTER_SILENT = "The adapter is connected but does not respond. Unplug it for 10 seconds, plug it back in, then try again."
HINT_IGNITION = "Turn the ignition on (engine off), wait a few seconds, then try again."
HINT_SESSION_REFUSED = "The engine computer refused a diagnostic session. overrev is only tested on the 991.2 GT3 DME."
SECURITY_HINT = (
    "On the tested 991.2 GT3 these values answer without SecurityAccess, so a refusal suggests "
    "a different DME or software version. This tool never sends SecurityAccess."
)
HINT_READ_INTERRUPTED = "The car stopped answering during the read. Keep the ignition on and try again."


def hint(key: str, exc: BaseException) -> str | None:
    """What to try next when step ``key`` failed with ``exc``, or None when the error says it already."""
    if key == ADAPTER:
        return HINT_ADAPTER_SILENT if isinstance(exc, TimeoutError) else None
    if key == CAR:
        if isinstance(exc, NegativeResponse):
            return HINT_SESSION_REFUSED
        if isinstance(exc, TimeoutError):
            return HINT_IGNITION
        if isinstance(exc, TransportError) and ("CAN ERROR" in str(exc) or "UNABLE TO CONNECT" in str(exc)):
            return HINT_IGNITION
        return None
    if key == READ:
        if isinstance(exc, NegativeResponse):
            return SECURITY_HINT if exc.request[0] == 0x22 and exc.nrc in (0x22, 0x33, 0x7E, 0x7F) else None
        if isinstance(exc, (TimeoutError, TransportError)):
            return HINT_READ_INTERRUPTED
    return None


class Steps:
    def __init__(self, out=None):
        self._out = out
        self.reported = None  # the exception a step already printed

    @contextmanager
    def step(self, key: str):
        ok_text, fail_text = TEXT[key]
        out = self._out or sys.stderr
        ok_mark, fail_mark = _marks(out)
        try:
            yield
        except Exception as e:
            print(f"{fail_mark} {fail_text}", file=out)
            print(f"error: {e}", file=out)
            if (text := hint(key, e)) is not None:
                print(f"hint: {text}", file=out)
            self.reported = e
            raise
        print(f"{ok_mark} {ok_text}", file=out)


def _marks(out) -> tuple[str, str]:
    try:
        "✓✗".encode(getattr(out, "encoding", None) or "ascii")
    except (UnicodeEncodeError, LookupError):
        return "ok", "FAIL"
    return "✓", "✗"
