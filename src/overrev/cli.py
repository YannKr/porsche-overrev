"""overrev: read Porsche DME over-rev counters (read-only)."""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict

from .bluetooth import open_adapter
from .decode import parse_fault_codes, read_over_revs, read_peak
from .transport import LiveTransport, ReadOnlyViolation, ReplayTransport, TransportError, open_elm
from .uds import NegativeResponse, ProtocolError, UdsClient

SECURITY_HINT = (
    "hint: on the tested 991.2 GT3 these DIDs answer without SecurityAccess, so a refusal "
    "suggests a different DME or software version. This tool never sends SecurityAccess."
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="overrev", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    read = sub.add_parser("read", help="read ignition counts and time counters for ranges 1-6")
    source = read.add_mutually_exclusive_group()
    source.add_argument("--mac", help="Bluetooth address of the OBDLink (default: find or pair one)")
    source.add_argument("--channel", help="CAN channel, e.g. can0")
    source.add_argument("--elm", metavar="PORT", help="serial port of an OBDLink/ELM327 adapter, e.g. /dev/rfcomm0")
    source.add_argument("--replay", metavar="LOG", help="serve responses from a recorded CAN log (CL2000 format) instead of a car")
    read.add_argument("--interface", default="socketcan", help="python-can interface type (default: socketcan)")
    read.add_argument("--json", action="store_true", help="print JSON instead of a table")
    auto = sub.add_parser("auto", help="unattended: wait for the adapter, read once, log, post to Discord")
    auto.add_argument("--elm", required=True, metavar="PORT", help="serial port of the OBDLink, e.g. /dev/rfcomm0")
    auto.add_argument("--vin", action="append", default=[],
                      help="VIN the DIDs were verified on (repeatable); other cars are read but flagged")
    auto.add_argument("--log-dir", required=True, help="where readings.jsonl and overrev.log go")
    auto.add_argument("--webhook-file", help="file holding a Discord webhook URL")
    auto.add_argument("--retry", type=float, default=30.0, help="seconds between attempts (default: 30)")
    return parser


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    if not argv or argv[0].startswith("-") and argv[0] not in ("-h", "--help"):
        argv = ["read", *argv]  # a bare `overrev` reads
    args = build_parser().parse_args(argv)
    if args.command == "auto":
        from . import auto
        return auto.main(args)
    try:
        if args.replay:
            transport = ReplayTransport(args.replay)
        elif args.elm:
            transport = open_elm(args.elm)
        elif args.channel:
            transport = LiveTransport(args.channel, args.interface)
        else:
            transport = open_adapter(args.mac, say=lambda msg: print(msg, file=sys.stderr))
        with transport:
            client = UdsClient(transport)
            client.start_session()
            readings = read_over_revs(client)
            try:
                peak = read_peak(client)
            except ProtocolError:
                peak = None  # a DME without the peak record
            try:
                codes, codes_note = parse_fault_codes(client.read_fault_codes()), ""
            except ProtocolError as e:
                codes, codes_note = None, str(e)
    except NegativeResponse as e:
        print(f"error: {e}", file=sys.stderr)
        if e.request[0] == 0x22 and e.nrc in (0x22, 0x33, 0x7E, 0x7F):
            print(SECURITY_HINT, file=sys.stderr)
        return 1
    except (TimeoutError, ProtocolError, TransportError, ReadOnlyViolation, OSError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps({
            "ranges": [asdict(r) for r in readings],
            "peak": asdict(peak) if peak else None,
            "fault_codes": [dict(asdict(c), meaning=c.describe()) for c in codes] if codes is not None else None,
        }, indent=2))
    else:
        print(f"{'Range':>5}  {'Ignitions':>9}  {'Time (min)':>10}  {'raw count':>9}  {'raw time':>8}")
        for r in readings:
            print(f"{r.range:>5}  {r.ignitions:>9}  {r.time_min:>10}  {r.count_raw:>9}  {r.time_raw:>8}")
        if peak:
            print(f"\nPeak engine speed: {peak.max_rpm:g} rpm. Time above the over-speed limit: {peak.over_speed_s:.1f} s.")
        else:
            print("\nPeak record: not available on this DME.")
        if codes is None:
            print(f"Fault memory: not available ({codes_note}).")
        elif not codes:
            print("Stored fault codes: none.")
        else:
            print(f"Stored fault codes ({len(codes)}):")
            for c in codes:
                print(f"  {c.code}  {c.describe()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
