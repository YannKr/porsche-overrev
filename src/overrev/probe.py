"""overrev probe: what a DME answers, as Markdown with no VIN or serial number, for cars overrev was not tested on."""
from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

from .decode import OVER_SPEED_DID, PART_NUMBER_DID, PEAK_RPM_DID, RANGES, SOFTWARE_DID, Identity, parse_fault_codes, text_field
from .transport import TransportError
from .uds import NegativeResponse, ProtocolError, UdsClient

HARDWARE_PART_DID = 0xF191
HARDWARE_VERSION_DID = 0xF1A3
VARIANT_DID = 0xF19E

# Never asked: F190 (VIN) and the serial-number DIDs. The report is pasted into a public issue.
COUNTER_DIDS = [
    entry for r in RANGES for entry in (
        (r.count_did, f"Range {r.number} ignitions"),
        (r.time_did, f"Range {r.number} op. time at last event"),
    )
] + [(PEAK_RPM_DID, "Peak engine speed"), (OVER_SPEED_DID, "Time above the over-speed limit")]


def _ask(read):
    """(data, None) for an answer, or (None, text to show) for a refusal, silence or an adapter error."""
    try:
        return read(), None
    except NegativeResponse as e:
        return None, f"NRC 0x{e.nrc:02X}"
    except TimeoutError:
        return None, "no answer"
    except TransportError as e:  # e.g. CAN ERROR on one DID: show it in the table and ask the next one
        return None, f"adapter error: {e}"


def _field(client: UdsClient, did: int):
    """(text or None, table cell) for an identification DID."""
    data, failure = _ask(lambda: client.read_did(did))
    text = None if failure else text_field(data)
    shown = failure or text or "empty"
    return text, shown.replace("|", "\\|")


def _answer(client: UdsClient, did: int) -> str:
    data, failure = _ask(lambda: client.read_did(did))
    if failure:
        return failure
    if not data:
        return "empty"
    hex_ = data.hex().upper()
    return f"{hex_} ({int.from_bytes(data, 'big')})" if len(data) <= 4 else hex_


def _fault_codes(client: UdsClient) -> str:
    data, failure = _ask(client.read_fault_codes)
    if failure:
        return failure
    try:
        n = len(parse_fault_codes(data))
    except ProtocolError:
        return data.hex().upper()
    return f"{n} code" if n == 1 else f"{n} codes"


def _version() -> str:
    try:
        return version("overrev")
    except PackageNotFoundError:
        return "unknown"


def run_probe(client: UdsClient) -> str:
    """Ask the DME the identification and over-rev DIDs, in a session already started; return the Markdown report."""
    part, part_shown = _field(client, PART_NUMBER_DID)
    software, software_shown = _field(client, SOFTWARE_DID)
    hardware_part = _field(client, HARDWARE_PART_DID)[1]
    hardware_version = _field(client, HARDWARE_VERSION_DID)[1]
    variant = _field(client, VARIANT_DID)[1]
    identity = Identity(part, software)
    lines = [
        "### overrev probe",
        "",
        f"overrev {_version()}",
        "",
        "| Field | Value |",
        "|---|---|",
        f"| Part number (F187) | {part_shown} |",
        f"| Software (F189) | {software_shown} |",
        f"| Hardware part number (F191) | {hardware_part} |",
        f"| Hardware version (F1A3) | {hardware_version} |",
        f"| Variant (F19E) | {variant} |",
        f"| Tested DME | {'yes' if identity.tested else 'no'} |",
        "",
        "| DID | Meaning on the tested DME | Answer |",
        "|---|---|---|",
    ]
    lines += [f"| {did:04X} | {meaning} | {_answer(client, did)} |" for did, meaning in COUNTER_DIDS]
    lines.append(f"| 19 02 2C | Stored fault codes | {_fault_codes(client)} |")
    return "\n".join(lines)
