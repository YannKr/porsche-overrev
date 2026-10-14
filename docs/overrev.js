// overrev in the browser: the read-only ELM327 / UDS / decode path of the Python CLI, ported line for line.
// No dependencies. The page (app.js) hands in an io object: { write(Uint8Array), read() -> Uint8Array | null }.

// --- errors (the Python exception classes) ----------------------------------------

class OverrevError extends Error {
  constructor(message) {
    super(message);
    this.name = new.target.name;
  }
}

export class ReadOnlyViolation extends OverrevError {}
export class TransportError extends OverrevError {}
export class TimeoutError extends OverrevError {}
export class ProtocolError extends OverrevError {}

const NRC_NAMES = {
  0x10: "generalReject",
  0x11: "serviceNotSupported",
  0x12: "subFunctionNotSupported",
  0x13: "incorrectMessageLengthOrInvalidFormat",
  0x21: "busyRepeatRequest",
  0x22: "conditionsNotCorrect",
  0x24: "requestSequenceError",
  0x31: "requestOutOfRange",
  0x33: "securityAccessDenied",
  0x7e: "subFunctionNotSupportedInActiveSession",
  0x7f: "serviceNotSupportedInActiveSession",
};

export class NegativeResponse extends ProtocolError {
  constructor(request, nrc) {
    super(`negative response to ${hex(request)}: NRC 0x${hex2(nrc)} ${NRC_NAMES[nrc] ?? "unknown"}`);
    this.request = request;
    this.nrc = nrc;
  }
}

const hex = bytes => Array.from(bytes, b => b.toString(16).padStart(2, "0")).join("");
const hex2 = n => n.toString(16).toUpperCase().padStart(2, "0");

// --- read-only allowlist (transport.check_read_only) ------------------------------

const ALLOWED_SUBFUNCTIONS = { 0x10: [0x01, 0x03], 0x3e: [0x00] };

export function checkReadOnly(payload) {
  const sid = payload.length ? payload[0] : null;
  if (sid === 0x22 && payload.length === 3) return;
  if (sid === 0x19 && payload.length === 3 && payload[1] === 0x02) return;  // read stored fault codes; clearing (14) stays blocked
  if (sid in ALLOWED_SUBFUNCTIONS && payload.length === 2 && ALLOWED_SUBFUNCTIONS[sid].includes(payload[1])) return;
  throw new ReadOnlyViolation(
    `refusing to transmit ${hex(payload) || "<empty>"}: only 10 01/03, 22 <DID>, 3E 00 and 19 02 <mask> are permitted`);
}

// --- OBDLink / ELM327 (transport.ElmTransport) ------------------------------------

const TESTER_ID = "7E0";
const ECU_ID = "7E8";
// None of these transmit on the bus. ATSP6 pins ISO 15765-4 11-bit 500 kbps: protocol auto-search
// would broadcast OBD 01 00 on 7DF. ATST FF waits ~1 s for the first frame of an answer.
const SETUP = ["ATE0", "ATH0", "ATSP6", "ATSTFF", `ATSH${TESTER_ID}`, `ATCRA${ECU_ID}`];
const TIMED_OUT = Symbol("timed out");
const now = () => performance.now();

function fromHex(text) {
  // bytes.fromhex: pairs of hex digits, ASCII whitespace allowed between pairs.
  const out = [];
  for (let i = 0; i < text.length;) {
    if (" \t\n\r\v\f".includes(text[i])) {
      i += 1;
      continue;
    }
    const pair = text.slice(i, i + 2);
    if (!/^[0-9A-Fa-f]{2}$/.test(pair)) throw new SyntaxError(`not hex: ${text}`);
    out.push(parseInt(pair, 16));
    i += 2;
  }
  return Uint8Array.from(out);
}

export class Elm {
  // The adapter does ISO-TP itself: a request goes out as one line of hex, each answer comes back as
  // one line, then the '>' prompt. Answers are handed up line by line so a 7F xx 78 reaches Uds
  // before the final answer. A multi-frame answer arrives as its length, then one "<index>:" line per frame.

  constructor(io, { commandTimeoutMs = 3000, resetTimeoutMs = 2500, resetTries = 3 } = {}) {
    this._io = io;
    this._commandTimeoutMs = commandTimeoutMs;
    this._resetTimeoutMs = resetTimeoutMs;  // the MX+ answers ATZ in ~1.8 s
    this._resetTries = resetTries;  // a command sent while the Bluetooth link is still coming up is lost
    this._buf = "";
    this._busy = false;
    this._reading = null;  // the one outstanding io.read(): a Web Serial read cannot be cancelled and kept
  }

  static async open(io, options = {}) {
    const elm = new Elm(io, options);
    try {
      await elm._reset();
      for (const cmd of SETUP) {
        const lines = await elm._command(cmd, elm._commandTimeoutMs);
        if (lines.at(-1) !== "OK") {
          throw new TransportError(`adapter rejected ${cmd}: ${lines.join(" / ") || "no reply"}`);
        }
      }
    } catch (error) {
      await elm.close();
      throw error;
    }
    return elm;
  }

  async _reset() {
    // ATZ answers with a banner (ELM327 v1.4b), not OK: reaching the prompt is enough.
    for (let attempt = 1; ; attempt++) {
      try {
        await this._command("ATZ", this._resetTimeoutMs);
        return;
      } catch (error) {
        if (!(error instanceof TimeoutError) || attempt === this._resetTries) throw error;
      }
    }
  }

  async _command(cmd, timeoutMs) {
    await this._write(cmd);
    const deadline = now() + timeoutMs;
    const lines = [];
    for (let line; (line = await this._readLine(deadline)) !== null;) lines.push(line);
    return lines;
  }

  async _write(text) {
    await this._io.write(new TextEncoder().encode(`${text}\r`));
    this._busy = true;
  }

  async _fill(deadline) {
    const remaining = deadline - now();
    if (remaining <= 0) throw new TimeoutError("no answer from the adapter");
    if (!this._reading) {
      this._reading = this._io.read();
      this._reading.catch(() => {});  // nobody may be waiting when it fails; whoever reads next sees the error
    }
    let timer;
    const timeout = new Promise(resolve => { timer = setTimeout(resolve, remaining, TIMED_OUT); });
    let chunk;
    try {
      chunk = await Promise.race([this._reading, timeout]);
    } finally {
      clearTimeout(timer);
    }
    if (chunk === TIMED_OUT) throw new TimeoutError("no answer from the adapter");
    this._reading = null;
    if (chunk === null) throw new TransportError("the adapter closed the connection");
    for (const b of chunk) this._buf += b < 0x80 ? String.fromCharCode(b) : "�";
  }

  async _readLine(deadline) {
    // Next non-empty line from the adapter, or null once the '>' prompt is read.
    // Lines end in \r or \r\n (the adapter's linefeed setting is left alone).
    for (;;) {
      this._buf = this._buf.replace(/^[\r\n\0]+/, "");
      if (this._buf.startsWith(">")) {
        this._buf = this._buf.slice(1);
        this._busy = false;
        return null;
      }
      const end = this._buf.indexOf("\r");
      if (end >= 0) {
        const line = this._buf.slice(0, end).replace(/[\0\n]/g, "").trim();
        this._buf = this._buf.slice(end + 1);
        if (line) return line;
        continue;
      }
      await this._fill(deadline);
    }
  }

  async send(payload) {
    payload = Uint8Array.from(payload);
    checkReadOnly(payload);
    const deadline = now() + this._commandTimeoutMs;
    try {
      while (this._busy && (await this._readLine(deadline)) !== null);  // the rest of the previous answer
    } catch (error) {
      if (error instanceof TimeoutError) throw new TransportError("adapter never returned to its prompt");
      throw error;
    }
    await this._write(hex(payload).toUpperCase());
  }

  async recv(timeoutMs) {
    if (!this._busy) throw new TimeoutError("adapter finished without (another) answer");
    const deadline = now() + timeoutMs;
    const line = await this._readLine(deadline);
    if (line === null || line === "NO DATA") throw new TimeoutError(`adapter: ${line ?? "no answer"}`);
    try {
      if (line.length === 3) {  // multi-frame: ISO-TP length, then "0:", "1:", ... lines
        if (!/^[0-9A-Fa-f]{3}$/.test(line)) throw new SyntaxError(line);
        return await this._readFrames(parseInt(line, 16), deadline);
      }
      return fromHex(line);  // tolerates the adapter's byte spacing
    } catch (error) {
      if (error instanceof SyntaxError) throw new TransportError(`adapter: ${line}`);
      throw error;
    }
  }

  async _readFrames(length, deadline) {
    const data = [];
    let index = 0;
    while (data.length < length) {
      const line = await this._readLine(deadline);
      if (line === null) {
        throw new TransportError(`adapter: multi-frame answer cut short at ${data.length} of ${length} bytes`);
      }
      const colon = line.indexOf(":");
      const head = colon < 0 ? line : line.slice(0, colon);
      const body = colon < 0 ? "" : line.slice(colon + 1);
      if (head.trim() !== index.toString(16).toUpperCase()) {
        throw new TransportError(`adapter: expected frame ${index.toString(16).toUpperCase()}, got ${line}`);
      }
      data.push(...fromHex(body));
      index = (index + 1) % 16;
    }
    return Uint8Array.from(data.slice(0, length));
  }

  async close() {
    await this._io.close?.();
  }
}

// --- UDS (uds.UdsClient) ----------------------------------------------------------

const SID_SESSION = 0x10;
const SID_READ_DID = 0x22;
const SID_TESTER_PRESENT = 0x3e;
const SID_READ_DTC = 0x19;
const DTC_BY_STATUS_MASK = 0x02;
const NEGATIVE = 0x7f;
const NRC_PENDING = 0x78;
const FINAL = "final";
const PENDING = "pending";
const OTHER = "other";

function classify(request, response) {
  // Is response the final answer to request, a response-pending, or unrelated?
  const sid = request[0];
  if (response.length >= 3 && response[0] === NEGATIVE && response[1] === sid) {
    return response[2] === NRC_PENDING ? PENDING : FINAL;
  }
  if (response.length && response[0] === sid + 0x40) {
    if (sid === SID_READ_DID) return response[1] === request[1] && response[2] === request[2] ? FINAL : OTHER;
    if (sid === SID_SESSION || sid === SID_TESTER_PRESENT) return response[1] === request[1] ? FINAL : OTHER;
    return FINAL;
  }
  return OTHER;
}

export class Uds {
  // The DME's 50 03 reply advertises P2 = 50 ms and P2* = 5000 ms. Both get a margin here (adapter
  // latency; the DME has been seen to re-send 7F 22 78 after 4.98 s). P2* restarts on every 7F xx 78.
  constructor(transport, { p2 = 1000, p2Star = 6000, keepaliveIdle = 1500, clock = now } = {}) {
    this._transport = transport;
    this._p2 = p2;
    this._p2Star = p2Star;
    this._keepaliveIdle = keepaliveIdle;  // send 3E 00 before a request if the bus was idle this long
    this._clock = clock;
    this._lastTx = null;
  }

  async request(payload) {
    payload = Uint8Array.from(payload);
    if (payload[0] !== SID_TESTER_PRESENT && this._lastTx !== null
        && this._clock() - this._lastTx > this._keepaliveIdle) {
      await this.testerPresent();
    }
    await this._transport.send(payload);
    this._lastTx = this._clock();
    let deadline = this._clock() + this._p2;
    for (;;) {
      const remaining = deadline - this._clock();
      if (remaining <= 0) throw new TimeoutError(`no response to ${hex(payload)}`);
      let response;
      try {
        response = await this._transport.recv(remaining);
      } catch (error) {
        if (error instanceof TimeoutError) throw new TimeoutError(`no response to ${hex(payload)}`);
        throw error;
      }
      const kind = classify(payload, response);
      if (kind === PENDING) {
        deadline = this._clock() + this._p2Star;
      } else if (kind === FINAL) {
        if (response[0] === NEGATIVE) throw new NegativeResponse(payload, response[2]);
        return response;
      }
      // OTHER: a stale or unrelated message; keep waiting for ours.
    }
  }

  startSession() {
    return this.request([SID_SESSION, 0x03]);
  }

  testerPresent() {
    return this.request([SID_TESTER_PRESENT, 0x00]);
  }

  readFaultCodes(mask = 0x2c) {
    // 19 02 <mask>; 2C = pending, confirmed, failed since last clear (the mask a dealer tester uses).
    return this.request([SID_READ_DTC, DTC_BY_STATUS_MASK, mask]);
  }

  async readDid(did) {
    const response = await this.request([SID_READ_DID, did >> 8, did & 0xff]);
    return response.subarray(3);
  }
}

// --- decoding (decode.py), verified against PIWIS on a 991.2 GT3 ----------------------

export const TABLE_EXPLANATION = "Op. h at last event: the engine's operating hours when that range was last reached.";

// [range, count DID (ignitions, x1), time DID (operating-time counter, x6 = minutes)]
export const RANGES = [
  [1, 0x1b68, 0x1741],
  [2, 0x1b69, 0x1743],
  [3, 0x1b6a, 0x1745],
  [4, 0x1bbb, 0x1bb8],
  [5, 0x1bbc, 0x1bb9],
  [6, 0x1bbd, 0x1bba],
];
export const TIME_SCALE_MIN = 6;

function u16(data) {
  if (data.length !== 2) throw new ProtocolError(`expected a 2-byte value, got ${hex(data) || "<empty>"}`);
  return (data[0] << 8) | data[1];
}

async function readOverRevs(uds) {
  const readings = [];
  for (const [range, countDid, timeDid] of RANGES) {
    const countData = await uds.readDid(countDid);
    const timeData = await uds.readDid(timeDid);
    const count = u16(countData);
    const time = u16(timeData);
    const time_min = time * TIME_SCALE_MIN;
    readings.push({ range, ignitions: count, time_min, count_raw: count, time_raw: time, hours: time_min / 60 });
  }
  return readings;
}

// The DME's all-time peak record (EngPrt_nMaxSpd 0.5 rpm per bit, EngPrt_tiOverSpd 0.2 s per bit).
const PEAK_RPM_DID = 0x1b8a;
const OVER_SPEED_DID = 0x1b8b;

export class PeakReading {
  constructor(maxRpmRaw, overSpeedRaw) {
    this.max_rpm = maxRpmRaw / 2;
    this.over_speed_s = overSpeedRaw / 5;
    this.max_rpm_raw = maxRpmRaw;
    this.over_speed_raw = overSpeedRaw;
  }

  describe() {
    // String(rpm) is Python's :g here: at most 32767.5, so never more than 6 significant digits.
    return `Peak engine speed: ${this.max_rpm} rpm. Time above the over-speed limit: ${this.over_speed_s.toFixed(1)} s.`;
  }
}

async function readPeak(uds) {
  const rpm = await uds.readDid(PEAK_RPM_DID);
  const over = await uds.readDid(OVER_SPEED_DID);
  return new PeakReading(u16(rpm), u16(over));
}

export function describeStatus(status) {
  // The ISO 14229 DTC status byte in words.
  const parts = [[0x01, "active"], [0x08, "confirmed"], [0x04, "pending"]].filter(([bit]) => status & bit)
    .map(([, name]) => name);
  if (!parts.length && status & 0x20) parts.push("stored (failed since last clear)");
  return parts.join(", ") || `status 0x${hex2(status)}`;
}

export function parseFaultCodes(response) {
  // 59 02 <availability mask> then 4 bytes per code: DTC (3) + status (1).
  const records = response.subarray(3);
  if (response.length < 3 || records.length % 4) throw new ProtocolError(`malformed fault code answer ${hex(response)}`);
  const codes = [];
  for (let i = 0; i < records.length; i += 4) {
    codes.push({ code: hex(records.subarray(i, i + 3)).toUpperCase(), status: records[i + 3] });
  }
  return codes;
}

const PART_NUMBER_DID = 0xf187;
const SOFTWARE_DID = 0xf189;

// [part number, software] pairs the over-rev DIDs were verified on.
export const TESTED_DMES = [["99161869217", "3671"]];

export const ISSUE_LINK = "https://github.com/YannKr/porsche-overrev/issues/new?template=new-car.yml";
export const UNTESTED_NOTE = "This DME ({describe}) has not been tested with overrev, so the values may not mean "
  + `what the labels say. Please share the output of 'overrev probe': ${ISSUE_LINK}`;

export function textField(data) {
  // An identification DID as text: ASCII as is, nothing as null, anything else as hex.
  let start = 0;
  let end = data.length;
  while (start < end && (data[start] === 0 || data[start] === 0x20)) start += 1;
  while (end > start && (data[end - 1] === 0 || data[end - 1] === 0x20)) end -= 1;
  const text = data.subarray(start, end);
  if (!text.length) return null;
  if (text.every(b => b >= 0x20 && b < 0x7f)) return String.fromCharCode(...text);
  return hex(data).toUpperCase();
}

export function formatPartNumber(part) {
  // 99161869217 -> 991.618.692.17; anything that is not 11 digits is returned unchanged.
  return /^[0-9]{11}$/.test(part) ? `${part.slice(0, 3)}.${part.slice(3, 6)}.${part.slice(6, 9)}.${part.slice(9)}` : part;
}

export class Identity {
  constructor(partNumber, software) {
    this.part_number = partNumber;
    this.software = software;
  }

  get tested() {
    return TESTED_DMES.some(([part, software]) => part === this.part_number && software === this.software);
  }

  describe() {
    const part = this.part_number ? formatPartNumber(this.part_number) : "part number unknown";
    const software = this.software ? `software ${this.software}` : "software unknown";
    return `DME ${part}, ${software}`;
  }
}

export function untestedNote(identity) {
  // The CLI's "note:" for a DME that is not the tested one, or null.
  if (identity.tested) return null;
  const describe = identity.describe().replace(/^DME /, "");
  return UNTESTED_NOTE.replace("{describe}", () => describe);  // a function: car text is inserted literally, never as $ patterns
}

async function readText(uds, did) {
  try {
    return textField(await uds.readDid(did));
  } catch (error) {
    if (error instanceof ProtocolError || error instanceof TimeoutError) return null;  // refused or silent: unknown
    throw error;
  }
}

async function readIdentity(uds) {
  return new Identity(await readText(uds, PART_NUMBER_DID), await readText(uds, SOFTWARE_DID));
}

// --- steps and hints (steps.py) -----------------------------------------------------

export const ADAPTER = "adapter";
export const CAR = "car";
export const READ = "read";

export const TEXT = {
  [ADAPTER]: ["Connected to the adapter", "Could not connect to the adapter"],
  [CAR]: ["The car answers", "The car does not answer"],
  [READ]: ["Read the over-rev counters", "Could not read the over-rev counters"],
};

export const HINT_ADAPTER_SILENT = "The adapter is connected but does not respond. Unplug it for 10 seconds, plug it back in, then try again.";
export const HINT_IGNITION = "Turn the ignition on (engine off), wait a few seconds, then try again.";
export const HINT_SESSION_REFUSED = "The engine computer refused a diagnostic session. overrev is only tested on the 991.2 GT3 DME.";
export const SECURITY_HINT = "On the tested 991.2 GT3 these values answer without SecurityAccess, so a refusal suggests "
  + "a different DME or software version. This tool never sends SecurityAccess.";
export const HINT_READ_INTERRUPTED = "The car stopped answering during the read. Keep the ignition on and try again.";

export function hint(key, error) {
  // What to try next when step key failed with error, or null when the error says it already.
  if (key === ADAPTER) return error instanceof TimeoutError ? HINT_ADAPTER_SILENT : null;
  if (key === CAR) {
    if (error instanceof NegativeResponse) return HINT_SESSION_REFUSED;
    if (error instanceof TimeoutError) return HINT_IGNITION;
    if (error instanceof TransportError
        && (error.message.includes("CAN ERROR") || error.message.includes("UNABLE TO CONNECT"))) {
      return HINT_IGNITION;
    }
    return null;
  }
  if (key === READ) {
    if (error instanceof NegativeResponse) {
      return error.request[0] === 0x22 && [0x22, 0x33, 0x7e, 0x7f].includes(error.nrc) ? SECURITY_HINT : null;
    }
    if (error instanceof TimeoutError || error instanceof TransportError) return HINT_READ_INTERRUPTED;
  }
  return null;
}

// --- the read, as `overrev read` does it (without the VIN) ---------------------------------

export async function readCar(io, onStep, options = {}) {
  // -> { identity, ranges, peak, codes, codesNote }. onStep(key, "ok") or onStep(key, "fail", error, hint)
  // for adapter, car and read; a failed step's error is then thrown.
  const step = async (key, run) => {
    let value;
    try {
      value = await run();
    } catch (error) {
      onStep(key, "fail", error, hint(key, error));
      throw error;
    }
    onStep(key, "ok");
    return value;
  };
  const elm = await step(ADAPTER, () => Elm.open(io, options));
  try {
    const uds = new Uds(elm, options);
    await step(CAR, () => uds.startSession());
    return await step(READ, async () => {
      const identity = await readIdentity(uds);
      const ranges = await readOverRevs(uds);
      let peak = null;
      try {
        peak = await readPeak(uds);
      } catch (error) {
        if (!(error instanceof ProtocolError)) throw error;  // a DME without the peak record
      }
      let codes = null;
      let codesNote = "";
      try {
        codes = parseFaultCodes(await uds.readFaultCodes());
      } catch (error) {
        if (!(error instanceof ProtocolError)) throw error;
        codesNote = error.message;
      }
      return { identity, ranges, peak, codes, codesNote };
    });
  } finally {
    await elm.close();
  }
}
