"""One-off: print the DME identification fields (VIN, part numbers, software) through an OBDLink.

Read-only: 10 03, then 22 <DID> for each field.
Usage: python scripts/read_ident.py [PORT]   (without PORT it finds the OBDLink like `overrev` does)
"""
import sys

from overrev.bluetooth import open_adapter
from overrev.transport import TransportError, open_elm
from overrev.uds import NegativeResponse, UdsClient

DIDS = [
    (0xF190, "VIN"), (0xF187, "Porsche part number"), (0xF189, "software version"),
    (0xF191, "hardware part number"), (0xF1A3, "hardware version"), (0xF197, "system name"),
    (0xF19E, "ODX variant"), (0xF1A2, "ODX version"), (0xF17C, "FAZIT ID"),
    (0xF17E, "production change no."), (0xF1AD, "engine code"), (0xF15B, "programming fingerprints"),
    (0xF18C, "ECU serial number"),
]

with open_elm(sys.argv[1]) if len(sys.argv) > 1 else open_adapter() as t:
    client = UdsClient(t)
    client.start_session()
    for did, name in DIDS:
        try:
            v = client.read_did(did)
        except NegativeResponse as e:
            print(f"{did:04X}  {name:26} NRC 0x{e.nrc:02X}")
            continue
        except (TimeoutError, TransportError) as e:
            print(f"{did:04X}  {name:26} error: {e}")
            continue
        text = v.rstrip(b"\0")
        printable = text and all(32 <= b < 127 for b in text)
        print(f"{did:04X}  {name:26} {text.decode('ascii') if printable else v.hex()}")
