"""Unattended once-per-boot read: wait for the adapter, read VIN + over-revs, log, post to Discord."""
from __future__ import annotations

import json
import logging
import os
import socket
import sys
import time
import urllib.request
from dataclasses import asdict
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .decode import read_over_revs
from .transport import TransportError, open_elm
from .uds import ProtocolError, UdsClient

log = logging.getLogger(__name__)

VIN_DID = 0xF190
POST_TRIES = 3


def read_car(transport, allowed_vins) -> dict:
    """VIN and the twelve over-rev counters. A DME refusal is recorded, not raised."""
    client = UdsClient(transport)
    record = {"time": datetime.now().astimezone().isoformat(timespec="seconds"), "vin": None, "verified": False}
    try:
        client.start_session()
        record["vin"] = client.read_did(VIN_DID).decode("ascii", "replace").strip("\0 ")
        record["verified"] = record["vin"] in allowed_vins
        record["ranges"] = [asdict(r) for r in read_over_revs(client)]
    except ProtocolError as e:
        record["error"] = str(e)
    return record


def run(opener, allowed_vins, log_dir, post, retry=30.0, sleep=time.sleep) -> dict:
    """Retry until one read completes, append it to readings.jsonl, post it, return it."""
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
    message = format_message(record, previous)
    for n in range(1, POST_TRIES + 1):
        try:
            post(message)
            log.info("posted to Discord")
            break
        except OSError as e:
            log.warning("Discord post %d/%d failed: %s", n, POST_TRIES, e)
            if n < POST_TRIES:
                sleep(retry)
    return record


def _previous(path: Path, vin: str):
    if not path.exists():
        return None
    matches = [r for r in map(json.loads, path.read_text().splitlines()) if r.get("vin") == vin and "ranges" in r]
    return matches[-1] if matches else None


def format_message(record: dict, previous) -> str:
    when = record["time"].replace("T", " ")[:16]
    vin = f"…{record['vin'][-6:]}" if record["vin"] else "unreadable"
    lines = [f"**991.2 GT3 DME over-rev read** {when} ({socket.gethostname()}, OBDLink, read-only)"]
    if record["verified"]:
        lines.append(f"VIN {vin}")
    else:
        lines.append(f"**UNVERIFIED CAR**: VIN {vin} is not the car these DIDs were verified on; "
                     "the values below may mean nothing for this DME.")
    if "error" in record:
        lines.append(f"The DME refused the read: {record['error']}")
        return "\n".join(lines)
    before = {r["range"]: r["ignitions"] for r in previous["ranges"]} if previous else {}
    rows = [f"{'Range':>5}  {'Ignitions':>9}  {'New':>5}  {'Op. h at last event':>19}"]
    for r in record["ranges"]:
        new = f"+{r['ignitions'] - before[r['range']]}" if r["range"] in before else "-"
        rows.append(f"{r['range']:>5}  {r['ignitions']:>9}  {new:>5}  {r['time_min'] / 60:>19.1f}")
    lines.append("```\n" + "\n".join(rows) + "\n```")
    lines.append(f"New = ignitions since the read of {previous['time'][:16].replace('T', ' ')}." if previous
                 else "First logged read for this VIN.")
    return "\n".join(lines)


def discord_poster(url: str):
    def post(content: str) -> None:
        request = urllib.request.Request(url, data=json.dumps({"content": content}).encode(),
                                         headers={"Content-Type": "application/json", "User-Agent": "overrev"})
        urllib.request.urlopen(request, timeout=15).close()
    return post


def main(args) -> int:
    log_dir = Path(args.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    to_file = RotatingFileHandler(log_dir / "overrev.log", maxBytes=1_000_000, backupCount=5)
    to_file.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    to_journal = logging.StreamHandler(sys.stderr)
    to_journal.setLevel(logging.INFO)
    logging.basicConfig(level=logging.DEBUG, handlers=[to_file, to_journal])
    if args.webhook_file:
        post = discord_poster(Path(args.webhook_file).read_text().strip())
    else:
        def post(content):
            log.info("no --webhook-file, not posting")
    log.info("waiting for the adapter on %s", args.elm)
    run(lambda: open_elm(args.elm), set(args.vin), log_dir, post, retry=args.retry)
    return 0
