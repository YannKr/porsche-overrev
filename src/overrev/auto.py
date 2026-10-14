"""Unattended once-per-boot read: wait for the adapter, read VIN + over-revs, log, post to Discord or ntfy."""
from __future__ import annotations

import json
import logging
import os
import socket
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import asdict
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .bluetooth import open_adapter
from .decode import FaultCode, Identity, parse_fault_codes, read_identity, read_over_revs, read_peak, read_vin
from .transport import TransportError, open_elm
from .uds import ProtocolError, UdsClient

log = logging.getLogger(__name__)

POST_TRIES = 3


def read_car(transport, allowed_vins) -> dict:
    """VIN, DME identity and the twelve over-rev counters. A DME refusal is recorded, not raised."""
    client = UdsClient(transport)
    record = {"time": datetime.now().astimezone().isoformat(timespec="seconds"), "vin": None, "verified": False,
              "dme": None}
    try:
        client.start_session()
        record["vin"] = read_vin(client)  # None when refused or silent; the read goes on
        record["verified"] = record["vin"] in allowed_vins
        identity = read_identity(client)
        record["dme"] = dict(asdict(identity), tested=identity.tested)
        record["ranges"] = [asdict(r) for r in read_over_revs(client)]
        try:
            record["peak"] = asdict(read_peak(client))
        except ProtocolError:
            record["peak"] = None  # a DME without the peak record
        try:
            record["fault_codes"] = [asdict(c) for c in parse_fault_codes(client.read_fault_codes())]
        except ProtocolError:
            record["fault_codes"] = None
    except ProtocolError as e:
        record["error"] = str(e)
    return record


def run(opener, allowed_vins, log_dir, post, retry=30.0, sleep=time.sleep) -> dict:
    """Retry until one read completes, append it to readings.jsonl, post it (unless post is None), return it."""
    attempt = 0
    while True:
        attempt += 1
        try:
            with opener() as transport:
                record = read_car(transport, allowed_vins)
            break
        except (OSError, TransportError) as e:  # adapter asleep/taken/unplugged, ignition off
            log.info("attempt %d: %s; retrying in %.0f s", attempt, e, retry)
            sleep(retry)
    log.info("read: %s", json.dumps(record))
    path = Path(log_dir) / "readings.jsonl"
    previous = _previous(path, record["vin"])
    with open(path, "a") as f:
        f.write(json.dumps(record) + "\n")
        f.flush()
        os.fsync(f.fileno())
    if post is None:
        return record
    message = format_message(record, previous, check_car=bool(allowed_vins))
    for n in range(1, POST_TRIES + 1):
        try:
            post(message)
            log.info("posted")
            break
        except Exception as e:  # the record is on disk; a post failure must not make the unit re-read the car
            log.warning("post %d/%d failed: %s", n, POST_TRIES, e)
            if n < POST_TRIES:
                sleep(retry)
    return record


def _previous(path: Path, vin: str):
    if not path.exists():
        return None
    matches = [r for r in map(json.loads, path.read_text().splitlines()) if r.get("vin") == vin and "ranges" in r]
    return matches[-1] if matches else None


def format_message(record: dict, previous, check_car: bool) -> str:
    """check_car is False when no VIN was configured: then no car is "another" car."""
    when = record["time"].replace("T", " ")[:16]
    vin = f"…{record['vin'][-6:]}" if record["vin"] else "unreadable"
    lines = [f"**DME over-rev read** {when} ({socket.gethostname()}, read-only)"]
    if record.get("dme"):
        dme = record["dme"]
        identity = Identity(dme["part_number"], dme["software"])
        lines.append(f"{identity.describe()} (tested)" if dme["tested"] else
                     f"**UNTESTED DME**: {identity.describe()}; the values may not mean what the labels say.")
    if check_car and record["vin"] and not record["verified"]:
        lines.append(f"**OTHER CAR**: VIN {vin} is not the car this was set up for.")
    else:
        lines.append(f"VIN {vin}")
    if "error" in record:
        lines.append(f"The DME refused the read: {record['error']}")
        return "\n".join(lines)
    before = {r["range"]: r["ignitions"] for r in previous["ranges"]} if previous else {}
    rows = [f"{'Range':>5}  {'Ignitions':>9}  {'New':>5}  {'Op. h at last event':>19}"]
    for r in record["ranges"]:
        new = f"+{r['ignitions'] - before[r['range']]}" if r["range"] in before else "-"
        rows.append(f"{r['range']:>5}  {r['ignitions']:>9}  {new:>5}  {r['time_min'] / 60:>19.1f}")
    lines.append("```\n" + "\n".join(rows) + "\n```")
    peak, before_peak = record.get("peak"), (previous or {}).get("peak")
    if peak:
        since = f" (+{peak['over_speed_s'] - before_peak['over_speed_s']:.1f} s since the previous read)" if before_peak else ""
        lines.append(f"Peak engine speed {peak['max_rpm']:g} rpm; {peak['over_speed_s']:.1f} s above the over-speed limit{since}.")
    else:
        lines.append("Peak record: not available on this DME.")
    codes = record.get("fault_codes")
    if codes is None:
        lines.append("Fault memory: not available.")
    else:
        listed = "; ".join(f"{c['code']} {FaultCode(**c).describe()}" for c in codes)
        lines.append(f"Stored fault codes ({len(codes)}): {listed}." if codes else "Stored fault codes: none.")
    lines.append(f"New = ignitions since the read of {previous['time'][:16].replace('T', ' ')}." if previous
                 else "First logged read for this VIN.")
    return "\n".join(lines)


def is_discord(url: str) -> bool:
    host = urllib.parse.urlsplit(url).hostname or ""
    return any(host == name or host.endswith("." + name) for name in ("discord.com", "discordapp.com"))


def poster(url: str):
    """Post a message to a Discord webhook (JSON) or, for any other URL such as an ntfy topic, as plain text."""
    try:
        parts = urllib.parse.urlsplit(url)
        valid = parts.scheme in ("http", "https") and bool(parts.hostname)
    except ValueError:
        valid = False
    if not valid:  # no URL in the message: a webhook is a secret
        raise ValueError("the webhook must be an http or https URL with a host, "
                         "a Discord webhook URL or an ntfy topic URL such as https://ntfy.sh/my-topic")
    discord = is_discord(url)

    def post(content: str) -> None:
        if discord:
            data = json.dumps({"content": content}).encode()
            headers = {"Content-Type": "application/json", "User-Agent": "overrev"}
        else:
            data = content.encode("utf-8")
            headers = {"Content-Type": "text/plain; charset=utf-8", "Title": "DME over-rev read",
                       "Markdown": "yes", "User-Agent": "overrev"}
        urllib.request.urlopen(urllib.request.Request(url, data=data, headers=headers), timeout=15).close()
    return post


def main(args) -> int:
    log_dir = Path(args.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    to_file = RotatingFileHandler(log_dir / "overrev.log", maxBytes=1_000_000, backupCount=5)
    to_file.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    to_journal = logging.StreamHandler(sys.stderr)
    to_journal.setLevel(logging.INFO)
    logging.basicConfig(level=logging.DEBUG, handlers=[to_file, to_journal])
    post = None
    if args.webhook_file:
        try:
            post = poster(Path(args.webhook_file).read_text().strip())
        except OSError as e:  # deleted to stop posting, or unreadable: the car is still read and logged
            log.warning("--webhook-file %s cannot be read (%s); not posting", args.webhook_file, e.strerror or e)
        except ValueError as e:  # before any read: a bad webhook must not make the unit re-read the car
            log.error("--webhook-file: %s", e)
            return 2
    else:
        log.info("no --webhook-file, not posting")
    if args.mac:
        log.info("waiting for the adapter %s", args.mac)
        opener = lambda: open_adapter(args.mac, say=log.info)
    else:
        log.info("waiting for the adapter on %s", args.elm)
        opener = lambda: open_elm(args.elm)
    run(opener, set(args.vin), log_dir, post, retry=args.retry)
    return 0
