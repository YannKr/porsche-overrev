# overrev: read-only over-rev counters for the Porsche 991.2 GT3 DME

> **Tested on one car only: a 2018 Porsche 991.2 GT3, US market**
> (PIWIS vehicle `GT3_991II_USA`, DME part number 991.618.692.17, DME
> software 3671). Other models, markets or DME software versions can use
> different identifiers or scales. In automatic mode, the tool flags any
> other VIN as unverified. Use it at your own risk.

The DME counts every ignition at an engine speed above the rev limit, in six
over-rev ranges. PIWIS shows these as `EngPrt_nrIgnNMax1..6` (number of
ignitions) and `EngPrt_tiNMax1..6` (engine operating time at the most recent
over-rev in that range). This tool reads the same twelve values with an
OBDLink adapter or any SocketCAN interface. It shows them exactly as PIWIS
does. You can also read them with no code at all, in the OBDLink phone app.

On the test car, the live read matched a PIWIS screen of the same day on all
twelve values.

## What it reads

| Range | Ignitions (DID) | Op. time at last over-rev (DID) |
|---|---|---|
| 1 | `1B68` | `1741` |
| 2 | `1B69` | `1743` |
| 3 | `1B6A` | `1745` |
| 4 | `1BBB` | `1BB8` |
| 5 | `1BBC` | `1BB9` |
| 6 | `1BBD` | `1BBA` |

- Each value is one UDS `22 <DID>` read from the DME (CAN, 11-bit, 500 kbps,
  request `7E0`, response `7E8`). The answer is a 16-bit big-endian word.
- Ignitions: the raw word is the count.
- Operating time: the raw word is in tenths of an operating hour. Raw × 6 is
  the value in minutes that PIWIS shows. Raw ÷ 10 is hours.
- The time value is not a duration. It is the engine operating-time stamp of
  the most recent over-rev in that range. Ranges that one rev excursion swept
  through at the same time have the same stamp.

Example (the test car, October 2026):

```
Range  Ignitions  Time (min)  raw count  raw time
    1       3382       30948       3382      5158
    2       1135       29928       1135      4988
    3        664       27252        664      4542
    4        272       27252        272      4542
    5        166       27252        166      4542
    6        104        6312        104      1052
```

## Read-only by design

The transport refuses to send anything other than `10 01`, `10 03`,
`22 <one DID>` and `3E 00`. It raises an error before transmit for anything
else. The tool never sends SecurityAccess (`27`), writes, routines, resets or
clear-codes requests.

No SecurityAccess is needed. On the test car, the DME answered all twelve
DIDs in the extended session (`10 03`). It also answered in the default
session: checked directly for `1B68` and `1741`, and the OBDLink phone app
reads all twelve as plain mode-22 PIDs.

## Hardware

| Option | Status |
|---|---|
| OBDLink MX+ over Bluetooth, Linux (Raspberry Pi) | Tested live |
| OBDLink MX+ app on iPhone, user-defined PIDs | Tested live |
| OBDLink app on Android | Same file format; not tested |
| Any SocketCAN interface (CANable, USB2CAN, CAN HAT) | Tested offline only (python-can virtual bus); the SocketCAN path itself is untested |
| OBDLink MX+ on macOS (`/dev/cu.*`) | Not tested |

A passive BMW "ENET" cable does not work. The 991.2 diagnoses over CAN, not
Ethernet.

## Install

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[test]'
```

## One-off read with an OBDLink MX+ (Linux)

1. Pair the MX+ (one time only). Plug it in, turn the ignition on (engine
   off), and press the **Connect** button on the MX+. The BT LED blinks fast,
   and pairing mode stays on for 2 minutes. Then run
   `deploy/pair-obdlink.sh`. It finds the adapter, pairs and trusts it, and
   binds it to `/dev/rfcomm0`. The binding is lost at reboot. After a reboot
   run `sudo rfcomm bind 0 <MAC> 1` (no button press needed).
2. Read:

```bash
.venv/bin/overrev read --elm /dev/rfcomm0          # table
.venv/bin/overrev read --elm /dev/rfcomm0 --json   # JSON
```

To print the DME identification (VIN, part numbers, software version, engine
code, programming history):

```bash
.venv/bin/python scripts/read_ident.py /dev/rfcomm0
```

With a SocketCAN interface instead:

```bash
sudo ip link set can0 up type can bitrate 500000
.venv/bin/overrev read --channel can0
```

## Automatic mode (Raspberry Pi)

`overrev auto` reads once per boot without user input. It retries every
30 s until the adapter and the car answer. Then it reads the VIN and the
twelve counters, appends one JSON record to `readings.jsonl`, and can post
the result to a Discord webhook. The post shows the ignitions added since the
previous read. If the VIN is not the one you set, the read still happens, but
the post says **UNVERIFIED CAR**.

1. Pair the MX+ as above.
2. Create the configuration (it stays out of git, mode 600):

```bash
mkdir -p ~/.config/overrev && chmod 700 ~/.config/overrev
printf 'OVERREV_MAC=<adapter MAC>\nOVERREV_VIN=<your VIN>\n' > ~/.config/overrev/env
echo '<Discord webhook URL>' > ~/.config/overrev/webhook
chmod 600 ~/.config/overrev/*
```

3. Install the service. Edit `User=` and the paths in
   `deploy/overrev-auto.service` first if your user is not `pi` or the
   checkout is not `~/overrev`:

```bash
sudo cp deploy/overrev-auto.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable overrev-auto
```

To use it: power the Pi, plug in the MX+, turn the ignition on. To read again
without a reboot, run `sudo systemctl restart overrev-auto`.

Logs:

- `~/overrev/log/readings.jsonl`: one record per read.
- `~/overrev/log/overrev.log`: everything, including each line to and from
  the adapter. It rotates at 1 MB and keeps 5 old files.
- `journalctl -u overrev-auto`.

## Phone, no code (OBDLink app)

The DIDs also answer in the default session, so the OBDLink app's
user-defined PIDs can read them. `deploy/obdlink/overrev-12-pids.udpid`
holds all twelve in the app's own export format: header `ECM`, mode `22`,
equation `A*256+B` for counts and `(A*256+B)/10` for hours.

1. Get the file onto the phone (iCloud Drive, Files, mail).
2. In the app, go to **Settings → User-Defined PIDs → ⋮ → Import**. On the
   test iPhone, the import **replaced** the existing user-defined PIDs.
3. Connect to the car and open **PID Values** to see all twelve.

The phone app is not limited to reads. Its clear-codes buttons are outside
this tool's read-only guard.

A web page could do the same read from desktop Chrome or Edge, which can
reach a paired Bluetooth serial device through Web Serial. That is not built.
iPhone browsers cannot reach the MX+.

## Known quirks (OBDLink MX+)

- **One connection at a time.** A phone that is paired with the MX+ and in
  range takes the adapter whenever it is free. The Pi then gets "Host is
  down". Turn off Bluetooth on the phone, or unpair the MX+ from it.
- **Fast reconnects fail.** A new connection less than about 5 s after a
  disconnect often fails. The first command on a new link can also be lost,
  so the transport retries `ATZ`.
- **The adapter sleeps.** A few minutes after the car is off, the MX+ goes
  to sleep and drops Bluetooth.
- **No passive check is possible.** The diagnostic CAN carries no traffic of
  its own, so the tool cannot tell a parked car from a running one before it
  sends. While the MX+ is still awake after switch-off, each retry puts one
  unanswered request on the bus.
- **Long answers.** Multi-frame answers with 10 or more frames have not been
  seen yet. Their line numbering is not verified.

## Tests

```bash
.venv/bin/python -m pytest
```

The suite checks the following:

- The read-only allowlist blocks requests before transmit.
- The decoder applies the scales, and the CLI reads, reports refusals and
  replays recorded CAN logs.
- The CAN transport, on python-can's virtual bus, refuses before transmit and
  times out cleanly when the DME is silent.
- The OBDLink transport works against a fake adapter that follows the
  ELM327 text protocol. This covers echo, prompt, response-pending,
  multi-frame answers and lost commands.
- The automatic mode handles retries, a foreign VIN, DME refusals and failed
  Discord posts.

## License

MIT, see `LICENSE`.

## Disclaimer

This is not affiliated with or endorsed by Porsche. PIWIS and OBDLink are
trademarks of their owners. Diagnostic requests go to a running vehicle
control unit. This tool only reads, but you are responsible for how and where
you use it.
