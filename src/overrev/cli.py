"""overrev: read Porsche DME over-rev counters (read-only)."""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from datetime import datetime

from .bluetooth import open_adapter
from .decode import (ISSUE_LINK, TABLE_EXPLANATION, UNTESTED_NOTE, parse_fault_codes, read_identity, read_over_revs,
                     read_peak, read_vin)
from .probe import _version, run_probe
from .report import render, write
from .steps import ADAPTER, CAR, READ, SECURITY_HINT, Steps  # noqa: F401
from .transport import LiveTransport, ReadOnlyViolation, ReplayTransport, TransportError, open_elm
from .uds import ProtocolError, UdsClient


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
    read.add_argument("--report", metavar="FILE.html", help="also save the results as a one-page HTML report")
    probe = sub.add_parser("probe", help="print what the engine computer answers, without VIN or serial number, to share for a new car")
    source = probe.add_mutually_exclusive_group()
    source.add_argument("--mac", help="Bluetooth address of the OBDLink (default: find or pair one)")
    source.add_argument("--elm", metavar="PORT", help="serial port of an OBDLink/ELM327 adapter, e.g. /dev/rfcomm0")
    auto = sub.add_parser("auto", help="unattended: wait for the adapter, read once, log, post to Discord or ntfy")
    source = auto.add_mutually_exclusive_group(required=True)
    source.add_argument("--mac", help="Bluetooth address of the OBDLink")
    source.add_argument("--elm", metavar="PORT", help="serial port of the OBDLink, e.g. /dev/rfcomm0")
    auto.add_argument("--vin", action="append", default=[],
                      help="VIN this was set up for (repeatable); other cars are read but flagged")
    auto.add_argument("--log-dir", required=True, help="where readings.jsonl and overrev.log go")
    auto.add_argument("--webhook-file", help="file holding a Discord webhook URL or an ntfy topic URL")
    auto.add_argument("--retry", type=float, default=30.0, help="seconds between attempts (default: 30)")
    setup = sub.add_parser("setup-auto", help="set up the automatic read at every boot as a systemd user service (Linux)")
    setup.add_argument("--mac", help="Bluetooth address of the OBDLink (default: find or pair one)")
    setup.add_argument("--webhook", metavar="URL", help="https:// Discord webhook or ntfy topic URL to post each result to")
    setup.add_argument("--remove", action="store_true", help="stop and remove the automatic read")
    return parser


FAILURES = (TimeoutError, ProtocolError, TransportError, ReadOnlyViolation, OSError, ValueError)


def say(message: str) -> None:
    print(message, file=sys.stderr)


def probe(args) -> int:
    steps = Steps()
    try:
        with steps.step(ADAPTER):
            transport = open_elm(args.elm) if args.elm else open_adapter(args.mac, say=say)
        with transport:
            client = UdsClient(transport)
            with steps.step(CAR):
                client.start_session()
            report = run_probe(client)
    except FAILURES as e:
        if e is not steps.reported:
            print(f"error: {e}", file=sys.stderr)
        return 1
    print(report)
    say(f"Paste this into a new issue (it holds no VIN or serial number): {ISSUE_LINK}")
    return 0


def save_report(path: str, **fields) -> int:
    try:
        saved = write(path, render(version=_version(), **fields))
    except OSError as e:
        print(f"error: cannot write the report: {e.strerror or e}", file=sys.stderr)
        return 1
    say(f"Report saved: {saved}")
    return 0


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    if not argv or argv[0].startswith("-") and argv[0] not in ("-h", "--help"):
        argv = ["read", *argv]  # a bare `overrev` reads
    args = build_parser().parse_args(argv)
    if args.command == "auto":
        from . import auto
        return auto.main(args)
    if args.command == "setup-auto":
        from . import setup_auto
        return setup_auto.main(args)
    if args.command == "probe":
        return probe(args)
    steps = Steps()
    try:
        with steps.step(ADAPTER):
            if args.replay:
                transport = ReplayTransport(args.replay)
            elif args.elm:
                transport = open_elm(args.elm)
            elif args.channel:
                transport = LiveTransport(args.channel, args.interface)
            else:
                transport = open_adapter(args.mac, say=say)
        with transport:
            client = UdsClient(transport)
            with steps.step(CAR):
                client.start_session()
            with steps.step(READ):
                identity = read_identity(client)
                vin = read_vin(client) if args.report else None
                readings = read_over_revs(client)
                try:
                    peak = read_peak(client)
                except ProtocolError:
                    peak = None  # a DME without the peak record
                try:
                    codes, codes_note = parse_fault_codes(client.read_fault_codes()), ""
                except ProtocolError as e:
                    codes, codes_note = None, str(e)
    except FAILURES as e:
        if e is not steps.reported:
            print(f"error: {e}", file=sys.stderr)
        return 1

    if not identity.tested:
        say("note: " + UNTESTED_NOTE.format(describe=identity.describe().removeprefix("DME ")))
    if args.json:
        print(json.dumps({
            "dme": dict(asdict(identity), tested=identity.tested),
            "ranges": [asdict(r) for r in readings],
            "peak": asdict(peak) if peak else None,
            "fault_codes": [dict(asdict(c), meaning=c.describe()) for c in codes] if codes is not None else None,
        }, indent=2))
    else:
        print(f"{identity.describe()} ({'tested' if identity.tested else 'not tested'})\n")
        print(f"{'Range':>5}  {'Ignitions':>9}  {'Op. h at last event':>19}")
        for r in readings:
            print(f"{r.range:>5}  {r.ignitions:>9}  {r.hours:>19.1f}")
        print(TABLE_EXPLANATION)
        if peak:
            print(f"\n{peak.describe()}")
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
    if args.report:
        return save_report(args.report, when=datetime.now().astimezone(), vin=vin, identity=identity,
                           readings=readings, peak=peak, codes=codes)
    return 0


if __name__ == "__main__":
    sys.exit(main())
