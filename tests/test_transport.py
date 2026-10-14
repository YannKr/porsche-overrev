import pytest

from overrev.transport import ReadOnlyViolation, Transport


class Recording(Transport):
    def __init__(self):
        self.sent = []

    def _send(self, payload):
        self.sent.append(payload)

    def recv(self, timeout):
        raise TimeoutError


def h(s):
    return bytes.fromhex(s)


@pytest.mark.parametrize("payload", [
    "2703",            # SecurityAccess requestSeed
    "2704a1b2c3d4",    # SecurityAccess sendKey
    "2e17410000",      # WriteDataByIdentifier
    "3b1741",          # WriteDataByLocalIdentifier
    "310101ff",        # RoutineControl
    "14ffffff",        # ClearDiagnosticInformation
    "04",              # OBD clear DTCs
    "1101",            # ECUReset
    "3401",            # RequestDownload
    "3501",            # RequestUpload
    "3601",            # TransferData
    "37",              # RequestTransferExit
    "1002",            # programming session
    "1060",            # a manufacturer-specific session
    "1003aa",          # wrong length
    "3e80",            # tester present with suppress bit: not on the allowlist
    "22174117 43",     # two DIDs in one request
    "22",              # no DID
    "",                # empty
])
def test_allowlist_blocks_before_transmit(payload):
    t = Recording()
    with pytest.raises(ReadOnlyViolation):
        t.send(h(payload.replace(" ", "")))
    assert t.sent == []


@pytest.mark.parametrize("payload", ["1001", "1003", "221741", "22f190", "3e00"])
def test_allowlist_permits_read_only_services(payload):
    t = Recording()
    t.send(h(payload))
    assert t.sent == [h(payload)]














