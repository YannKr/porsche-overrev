"""One-off, read-only: try 22 <DID> for every DID in a range and print the ones the DME answers.

Usage: python scripts/scan_dids.py 1B00 1BFF [PORT]   (without PORT it finds the OBDLink like `overrev` does)
Unsupported DIDs (NRC 31) are skipped silently. Compare the hits against a dealer tester's
"actual values" screen to give them names and scales.
"""
import sys

from overrev.bluetooth import open_adapter
from overrev.transport import TransportError, open_elm
from overrev.uds import NegativeResponse, UdsClient

first, last = int(sys.argv[1], 16), int(sys.argv[2], 16)
with open_elm(sys.argv[3]) if len(sys.argv) > 3 else open_adapter() as t:
    client = UdsClient(t)
    client.start_session()
    for did in range(first, last + 1):
        try:
            v = client.read_did(did)
        except NegativeResponse as e:
            if e.nrc != 0x31:
                print(f"{did:04X}  NRC 0x{e.nrc:02X}", flush=True)
            continue
        except (TimeoutError, TransportError) as e:
            print(f"{did:04X}  error: {e}", flush=True)
            continue
        number = f"{int.from_bytes(v, 'big'):>10}" if 0 < len(v) <= 4 else " " * 10
        text = "".join(chr(b) if 32 <= b < 127 else "." for b in v)
        print(f"{did:04X}  {len(v):>3} B  {v.hex():<24} {number}  {text}", flush=True)
