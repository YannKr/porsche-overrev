# DME over-rev protocol: 991.2 GT3 (US, `GT3_991II_USA`)

Tested on one car only: a 2018 991.2 GT3, US market, DME part number
991.618.692.17, DME software 3671. Every value below was read live and
matched the dealer tester's (PIWIS) "Actual values / input signals" screen
of the same day exactly.

## Bus and addressing

| Item | Value |
|---|---|
| CAN | 11-bit identifiers, 500 kbps |
| Tester → DME | `0x7E0` |
| DME → tester | `0x7E8` |
| Transport | ISO-TP, normal addressing, 8-byte frames |
| Padding | Ignore it: honour the ISO-TP length, not the byte value |
| Session | `10 03` → `50 03 00 32 01 F4` (P2 = 50 ms, P2\* = 5000 ms) |
| Keep-alive | `3E 00` → `7E 00` |
| Response pending | The DME sometimes answers `7F 22 78` first; the final answer usually follows within ~100 ms |

## Over-rev data

The dealer tester shows two arrays of six: `EngPrt_nrIgnNMax1..6`
(ignitions, no unit) and `EngPrt_tiNMax1..6` (shown in minutes). Each value
is one `22 <DID>` read. The answer is a 16-bit big-endian word.

### Ignition counts: scale ×1

| Range | DID | Raw word | Decimal | Tester |
|---|---|---|---|---|
| 1 | `1B68` | `0D36` | 3382 | 3382 |
| 2 | `1B69` | `046F` | 1135 | 1135 |
| 3 | `1B6A` | `0298` | 664 | 664 |
| 4 | `1BBB` | `0110` | 272 | 272 |
| 5 | `1BBC` | `00A6` | 166 | 166 |
| 6 | `1BBD` | `0068` | 104 | 104 |

### Operating time at the last over-rev: scale ×6 → minutes

| Range | DID | Raw word | Decimal | ×6 | Tester (min) |
|---|---|---|---|---|---|
| 1 | `1741` | `1426` | 5158 | 30948 | 30948 |
| 2 | `1743` | `137C` | 4988 | 29928 | 29928 |
| 3 | `1745` | `11BE` | 4542 | 27252 | 27252 |
| 4 | `1BB8` | `11BE` | 4542 | 27252 | 27252 |
| 5 | `1BB9` | `11BE` | 4542 | 27252 | 27252 |
| 6 | `1BBA` | `041C` | 1052 | 6312 | 6312 |

One raw unit is 6 min = 0.1 h. So the DME stores tenths of an operating
hour, and the tester shows minutes.

The value is not a duration. It is the engine operating-time stamp of the
most recent over-rev in that range. One rev excursion that reaches range 5
passes through ranges 3 and 4 at the same moment, so the three share a
stamp.

## Peak record

| DID | Tester signal | Scale | Test car (2026-10-13) |
|---|---|---|---|
| `1B8A` | `EngPrt_nMaxSpd`, highest engine speed ever | 0.5 rpm per bit | `55F0` = 11000 rpm |
| `1B8B` | `EngPrt_tiOverSpd`, total time above the over-speed limit | 0.2 s per bit | `0017` = 4.6 s |

Both matched the tester screen of the same day. The `1B8B` scale rests on
that one reading. The odometer at the peak (`EngPrt_lenMilgNMaxSpd`) and the
total operating hours are on the same tester screen, but their DIDs are not
known yet; `scripts/scan_dids.py` lists every DID in a range that answers.

## Stored fault codes

`19 02 2C` (ReadDTCInformation, report by status mask; mask `2C` = pending,
confirmed, failed since last clear). The answer is
`59 02 <availability mask>`, then 4 bytes per code: the 3-byte code and its
status byte. Status bits: `01` active, `04` pending, `08` confirmed, `20`
failed since the last clear. The tool shows the code as 6 hex digits and does
not map it to a fault name. Clearing (`14`) is never sent. Not yet verified
live in a plain `10 03` session.

## Session and security access

No SecurityAccess (`27`) is needed. On the test car the DME answered all
twelve DIDs in the extended session (`10 03`). It also answered in the
default session: checked directly for `1B68` and `1741`, and the OBDLink
phone app reads all twelve as plain mode-22 PIDs. The tool only ever sends
`10 01/03`, `22 <DID>`, `3E 00` and `19 02 <mask>`.

If a DME refuses a DID with NRC `33` (securityAccessDenied), `22`
(conditionsNotCorrect), `7E` or `7F` (not supported in active session), it
is probably a different DME or software version than the tested one.

## Identification DIDs

These standard identification DIDs answer on the same DME (`10 03`, then
`22 <DID>`). `scripts/read_ident.py` prints them.

| DID | Content | Test car |
|---|---|---|
| `F190` | VIN | (not published) |
| `F187` | Porsche part number | `99161869217` |
| `F189` | Software version | `3671` |
| `F191` | Hardware part number | `99161860302` |
| `F1A3` | Hardware version | `H51` |
| `F197` | System name | `P1319NB6GSUM` |
| `F19E` | ODX variant | `EV_ECM40FSI01199161869217` |
| `F1A2` | ODX version | `R10000` |
| `F1AD` | Engine code | `DGGA` |
| `F17C` | FAZIT ID (unique per ECU) | (not published) |
| `F17E` | Production change number | `81H51---` |
| `F15B` | Programming fingerprints | Date-stamped flash records |
| `F18C` | ECU serial number | Not supported (NRC `31`) |

## OBDLink MX+ (ELM327 command set) as the adapter

`overrev read --elm /dev/rfcomm0` drives an OBDLink (STN chip) over a
serial port instead of SocketCAN. The adapter does ISO-TP itself. Setup
after `ATZ`: `ATE0`, `ATH0`, `ATSP6`, `ATSTFF`, `ATSH7E0`, `ATCRA7E8`.
`ATSP6` pins ISO 15765-4, 11-bit, 500 kbps; protocol auto-search would
broadcast OBD `01 00` on `7DF`. `ATSTFF` waits ~1 s for the first frame.
None of these transmit on the bus. Then each UDS request is one line of
hex, and each answer comes back as one line before the `>` prompt.

- The STN keeps listening after `7F xx 78` by default (programmable
  parameter 2A = `04`, bit 2 "Processing 7F xx 78's"). Whether it also
  prints the pending line is not confirmed; the transport works either way.
- A multi-frame answer (VIN, part numbers) comes as its ISO-TP length on one
  line (`014`), then one `<index>:` line per CAN frame. A missing or
  out-of-order frame is an error, not misparsed.
- `NO DATA` = no answer within `ATST` (ignition off, wrong bus).
  `CAN ERROR` = not on a 500 kbps bus, or nothing on the bus is awake.
- Multi-frame line numbers are parsed as hex `0`-`F` (the ISO-TP sequence
  nibble). Only answers up to 8 frames (`F15B`, 53 bytes) were seen live.
  Whether the adapter wraps after `9` instead is unverified. An answer of
  10+ frames would then be refused with an error, never misread.

Pairing on a Pi (BlueZ): put the MX+ in pairing mode with its button,
then `bluetoothctl pair <MAC>`, `bluetoothctl trust <MAC>`,
`sudo rfcomm bind 0 <MAC> <SPP channel>` (find the channel with
`sdptool search --bdaddr <MAC> SP`). `/dev/rfcomm0` is `root:dialout`.

## CAN log replay (development)

`overrev read --replay LOG` serves DME answers from a recorded CAN log
instead of a car, for offline testing. Format: `Timestamp;Type;ID;Data`,
`Type 0` = data frame, IDs in lowercase hex, `Data` = 8 raw bytes. ISO-TP
PCI nibble: `0` single frame (low nibble = length), `1` first frame
(12-bit length), `2` consecutive frame (sequence), `3` flow control.
Messages are reassembled per CAN ID.

The replay transport attaches each `7E8` message to the most recent `7E0`
request. It keeps an exchange only if the exchange contains a final
response for that request: positive with a matching DID echo, or negative
with a code other than `78`. An exchange whose final answer landed after
the next tester request is dropped rather than misattributed. Recorded
`7F 22 78` → `62 …` sequences are replayed as recorded, so the pending path
is exercised offline.
