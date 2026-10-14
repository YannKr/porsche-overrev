"""Minimal UDS (ISO 14229) client: only the three read-only services this tool uses."""
from __future__ import annotations

import time

SID_SESSION = 0x10
SID_READ_DID = 0x22
SID_TESTER_PRESENT = 0x3E
NEGATIVE = 0x7F
NRC_PENDING = 0x78

FINAL, PENDING, OTHER = "final", "pending", "other"

NRC_NAMES = {
    0x10: "generalReject",
    0x11: "serviceNotSupported",
    0x12: "subFunctionNotSupported",
    0x13: "incorrectMessageLengthOrInvalidFormat",
    0x21: "busyRepeatRequest",
    0x22: "conditionsNotCorrect",
    0x24: "requestSequenceError",
    0x31: "requestOutOfRange",
    0x33: "securityAccessDenied",
    0x7E: "subFunctionNotSupportedInActiveSession",
    0x7F: "serviceNotSupportedInActiveSession",
}

# The DME's 50 03 reply advertises P2 = 50 ms and P2* = 5000 ms. Both get a
# margin here (adapter latency; the DME has been seen to re-send 7F 22 78
# after 4.98 s). P2* is restarted on every 7F xx 78.
P2_TIMEOUT = 1.0
P2_STAR_TIMEOUT = 6.0
KEEPALIVE_IDLE = 1.5  # send 3E 00 before a request if the bus was idle this long


class ProtocolError(Exception):
    pass


class NegativeResponse(ProtocolError):
    def __init__(self, request: bytes, nrc: int):
        self.request = request
        self.nrc = nrc
        super().__init__(
            f"negative response to {request.hex()}: NRC 0x{nrc:02X} {NRC_NAMES.get(nrc, 'unknown')}"
        )


def classify(request: bytes, response: bytes) -> str:
    """Is ``response`` the final answer to ``request``, a response-pending, or unrelated?"""
    sid = request[0]
    if len(response) >= 3 and response[0] == NEGATIVE and response[1] == sid:
        return PENDING if response[2] == NRC_PENDING else FINAL
    if response and response[0] == sid + 0x40:
        if sid == SID_READ_DID:
            return FINAL if response[1:3] == request[1:3] else OTHER
        if sid in (SID_SESSION, SID_TESTER_PRESENT):
            return FINAL if response[1:2] == request[1:2] else OTHER
        return FINAL
    return OTHER


class UdsClient:
    def __init__(self, transport, p2=P2_TIMEOUT, p2_star=P2_STAR_TIMEOUT,
                 keepalive_idle=KEEPALIVE_IDLE, clock=time.monotonic):
        self._transport = transport
        self._p2 = p2
        self._p2_star = p2_star
        self._keepalive_idle = keepalive_idle
        self._clock = clock
        self._last_tx = None

    def request(self, payload: bytes) -> bytes:
        payload = bytes(payload)
        if (payload[0] != SID_TESTER_PRESENT and self._last_tx is not None
                and self._clock() - self._last_tx > self._keepalive_idle):
            self.tester_present()
        self._transport.send(payload)
        self._last_tx = self._clock()
        deadline = self._clock() + self._p2
        while True:
            remaining = deadline - self._clock()
            if remaining <= 0:
                raise TimeoutError(f"no response to {payload.hex()}")
            try:
                response = bytes(self._transport.recv(remaining))
            except TimeoutError:
                raise TimeoutError(f"no response to {payload.hex()}") from None
            kind = classify(payload, response)
            if kind == PENDING:
                deadline = self._clock() + self._p2_star
            elif kind == FINAL:
                if response[0] == NEGATIVE:
                    raise NegativeResponse(payload, response[2])
                return response
            # OTHER: a stale or unrelated message; keep waiting for ours.

    def start_session(self) -> None:
        self.request(bytes((SID_SESSION, 0x03)))

    def tester_present(self) -> None:
        self.request(bytes((SID_TESTER_PRESENT, 0x00)))

    def read_did(self, did: int) -> bytes:
        response = self.request(bytes((SID_READ_DID, did >> 8, did & 0xFF)))
        return response[3:]
