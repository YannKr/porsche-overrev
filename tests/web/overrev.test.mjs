// The browser page's logic (docs/overrev.js, docs/app.js) against a fake OBDLink.
//
// FakeElm mirrors tests/test_elm.py: it honours echo, spaces, headers and line
// ends the way the adapter does, refuses input before its '>' prompt was read,
// and refuses a second read() while one is outstanding (a Web Serial read
// cannot be cancelled without releasing the reader). car() mirrors
// tests/test_auto.py. Run with: node --test tests/web/*.test.mjs
import { test } from "node:test";
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import {
  Elm, HINT_ADAPTER_SILENT, HINT_IGNITION, HINT_READ_INTERRUPTED, HINT_SESSION_REFUSED, ISSUE_LINK,
  NegativeResponse, ProtocolError, ReadOnlyViolation, SECURITY_HINT, TABLE_EXPLANATION, TESTED_DMES, TEXT,
  TimeoutError, TransportError, UNTESTED_NOTE, Uds, checkReadOnly, describeStatus, formatPartNumber, Identity,
  parseFaultCodes, readCar, textField, untestedNote,
} from "../../docs/overrev.js";
import { connect, copyButtons } from "../../docs/app.js";

const ROOT = fileURLToPath(new URL("../../", import.meta.url));
const enc = text => new TextEncoder().encode(text);
const dec = bytes => new TextDecoder().decode(bytes);
const h = text => Uint8Array.from(text.match(/../g) ?? [], pair => parseInt(pair, 16));
const hexOf = bytes => Array.from(bytes, b => b.toString(16).padStart(2, "0")).join("");
const HEX = bytes => hexOf(bytes).toUpperCase();
const hex4 = n => n.toString(16).toUpperCase().padStart(4, "0");

class FakeElm {
  constructor(respond, { atReplies = {}, swallow = 0, linefeeds = true, chunk = 5 } = {}) {
    this.respond = respond;  // request bytes -> list of reply lines (hex or adapter text)
    this.atReplies = atReplies;
    this.swallow = swallow;  // commands lost before the Bluetooth link is up
    this.chunk = chunk;  // bytes per read(), so lines arrive in pieces as over a real link
    this.defaultLinefeeds = linefeeds;
    this.reset();
    this.protocol = this.header = this.rxFilter = null;
    this.commands = [];
    this.requests = [];
    this.out = "";
    this.held = [];
    this.line = "";
    this.waiter = null;
    this.closed = false;
  }

  reset() {
    [this.echo, this.spaces, this.headers, this.linefeeds] = [true, true, false, this.defaultLinefeeds];
  }

  // io surface
  async write(data) {
    assert.ok(!this.out.includes(">") && !this.held.length, "write while the previous command is still running");
    for (const ch of dec(data)) {
      if (ch === "\r") {
        this.command(this.line);
        this.line = "";
      } else {
        this.line += ch;
      }
    }
  }

  read() {
    assert.equal(this.waiter, null, "a second read() while one is outstanding");
    if (this.out) return Promise.resolve(this.take());
    return new Promise(resolve => { this.waiter = resolve; });
  }

  close() {
    this.closed = true;
  }

  hangUp() {  // what reader.cancel() does to a pending read
    const waiter = this.waiter;
    this.waiter = null;
    waiter?.(null);
  }

  // adapter behaviour
  take() {
    const chunk = this.out.slice(0, this.chunk);
    this.out = this.out.slice(this.chunk);
    return enc(chunk);
  }

  push(text) {
    this.out += text;
    if (this.waiter && this.out) {
      const waiter = this.waiter;
      this.waiter = null;
      waiter(this.take());
    }
  }

  eol() {
    return this.linefeeds ? "\r\n" : "\r";
  }

  emit(lines, prompt = true) {
    this.push(lines.map(line => line + this.eol()).join("") + (prompt ? this.eol() + ">" : ""));
  }

  command(cmd) {
    if (this.swallow) {
      this.swallow -= 1;
      return;
    }
    this.commands.push(cmd);
    if (this.echo) this.push(cmd + this.eol());
    const c = cmd.replaceAll(" ", "").toUpperCase();
    if (c.startsWith("AT")) {
      this.emit([this.at(c.slice(2))]);
      return;
    }
    const request = h(c);
    this.requests.push(hexOf(request));
    const lines = this.respond(request);
    if (lines.at(-1) === "HOLD") {
      this.emit(lines.slice(0, -2).map(line => this.format(line)), false);
      this.held = [lines.at(-2)];
      return;
    }
    this.emit(lines.map(line => this.format(line)));
  }

  release() {
    const lines = this.held;
    this.held = [];
    this.emit(lines.map(line => this.format(line)));
  }

  format(line) {
    if (!/^([0-9A-Fa-f]{2})+$/.test(line)) return line;  // adapter text such as NO DATA, or a multi-frame line
    let data = h(line);
    if (this.headers) data = Uint8Array.from([0x07, 0xe8, data.length, ...data]);
    const text = HEX(data);
    return this.spaces ? text.match(/../g).join(" ") : text;
  }

  at(c) {
    if (c in this.atReplies) return this.atReplies[c];
    if (c === "Z") {
      this.reset();
      return "ELM327 v1.4b";
    }
    const flags = { E: "echo", S: "spaces", H: "headers", L: "linefeeds" };
    if (/^[ESHL][01]$/.test(c)) this[flags[c[0]]] = c[1] === "1";
    else if (c.startsWith("SP")) this.protocol = c.slice(2);
    else if (c.startsWith("SH")) this.header = c.slice(2);
    else if (c.startsWith("CRA")) this.rxFilter = c.slice(3);
    else if (!c.startsWith("ST")) return "?";
    return "OK";
  }
}

// Made-up VIN: the real one stays out of git.
const VIN_FRAMES = ["014", "0: 62 F1 90 57 50 30", "1: 5A 5A 5A 39 39 5A 4B", "2: 53 31 30 30 30 30 31"];
const COUNT_DIDS = ["1b68", "1b69", "1b6a", "1bbb", "1bbc", "1bbd"];
const TIME_DIDS = ["1741", "1743", "1745", "1bb8", "1bb9", "1bba"];

function didAnswer(did, data) {
  // The lines an ELM327 prints for a positive answer to 22 <did>: one line, or ISO-TP lines past 7 bytes.
  const payload = [0x62, did >> 8, did & 0xff, ...data];
  if (payload.length <= 7) return [HEX(payload)];
  const lines = [payload.length.toString(16).toUpperCase().padStart(3, "0"), "0:" + HEX(payload.slice(0, 6))];
  for (let start = 6, i = 1; start < payload.length; start += 7, i++) {
    const frame = payload.slice(start, start + 7);
    while (frame.length < 7) frame.push(0xaa);
    lines.push(`${(i % 16).toString(16).toUpperCase()}:${HEX(frame)}`);
  }
  return lines;
}

function car({
  counts = [3382, 1135, 664, 272, 166, 104], timesRaw = [5158, 4988, 4542, 4542, 4542, 1052], refuse = null,
  peakRaw = [0x55f0, 0x0017], dme = ["99161869217", "3671"],
} = {}) {
  // dme is [part number, software]; null for a field makes the DME refuse that DID.
  const table = {
    "1003": ["5003003201F4"], "22f190": VIN_FRAMES,
    "221b8a": [`621B8A${hex4(peakRaw[0])}`], "221b8b": [`621B8B${hex4(peakRaw[1])}`],
    "19022c": ["5902FF00ABCD60"],
  };
  [0xf187, 0xf189].forEach((did, i) => {
    table[`22${did.toString(16)}`] = dme[i] === null ? ["7F2231"] : didAnswer(did, enc(dme[i]));
  });
  COUNT_DIDS.forEach((did, i) => { table[`22${did}`] = [`62${did.toUpperCase()}${hex4(counts[i])}`]; });
  TIME_DIDS.forEach((did, i) => { table[`22${did}`] = [`62${did.toUpperCase()}${hex4(timesRaw[i])}`]; });
  if (refuse) table[`22${refuse}`] = ["7F2231"];
  return request => {
    const key = hexOf(request);
    assert.ok(key in table, `unexpected request ${key}`);
    return table[key];
  };
}

function withReply(requestHex, lines, respond = car()) {
  return request => (hexOf(request) === requestHex ? lines : respond(request));
}

const FAST = { resetTimeoutMs: 30 };

async function read(fake, options) {
  const steps = [];
  const outcome = await readCar(fake, (...args) => steps.push(args), options).then(
    result => ({ result }), error => ({ error }));
  return { ...outcome, steps };
}

const EVERY_REQUEST = [
  "1003", "22f187", "22f189",
  "221b68", "221741", "221b69", "221743", "221b6a", "221745",
  "221bbb", "221bb8", "221bbc", "221bb9", "221bbd", "221bba",
  "221b8a", "221b8b", "19022c",
];

// --- adapter setup -------------------------------------------------------------

test("setup pins the protocol and the DME addresses without bus traffic", async () => {
  const fake = new FakeElm(car());
  await Elm.open(fake);
  assert.deepEqual(fake.commands, ["ATZ", "ATE0", "ATH0", "ATSP6", "ATSTFF", "ATSH7E0", "ATCRA7E8"]);
  assert.equal(fake.protocol, "6");  // ISO 15765-4, 11-bit, 500 kbps; never auto-search
  assert.deepEqual([fake.header, fake.rxFilter], ["7E0", "7E8"]);
  assert.deepEqual(fake.requests, []);
});

test("setup retries a reset lost while the link comes up", async () => {
  const fake = new FakeElm(car(), { swallow: 1 });
  const uds = new Uds(await Elm.open(fake, FAST));
  assert.deepEqual(await uds.readDid(0x1b68), h("0d36"));
});

test("a rejected setup command is a transport error naming it, and closes the io", async () => {
  const fake = new FakeElm(car(), { atReplies: { SP6: "?" } });
  await assert.rejects(Elm.open(fake), error => error instanceof TransportError && /ATSP6/.test(error.message));
  assert.ok(fake.closed);
});

test("an adapter that never answers the reset fails after three tries and closes the io", async () => {
  const fake = new FakeElm(car(), { swallow: 99 });
  await assert.rejects(Elm.open(fake, FAST), TimeoutError);
  assert.equal(fake.swallow, 96);
  assert.ok(fake.closed);
});

// --- read-only allowlist -------------------------------------------------------

const REFUSED = ["2701", "14ffffff", "2ef19000", "3101ff00", "1101", "1002", "22f1", "", "22f19000", "100300", "3e01",
  "1901ff", "190a", "2702", "2e", "34", "3e"];

test("checkReadOnly refuses everything outside the allowlist", () => {
  for (const bad of REFUSED) assert.throws(() => checkReadOnly(h(bad)), ReadOnlyViolation, bad);
  for (const good of ["1001", "1003", "221b68", "22f190", "3e00", "19022c", "1902ff"]) checkReadOnly(h(good));
});

test("a refused request never reaches the adapter", async () => {
  const fake = new FakeElm(car());
  const elm = await Elm.open(fake);
  const sent = fake.commands.length;
  for (const bad of REFUSED) await assert.rejects(elm.send(h(bad)), ReadOnlyViolation);
  assert.equal(fake.commands.length, sent);
});

// --- ELM327 line rules ---------------------------------------------------------

test("a 7F 22 78 before the answer is waited through", async () => {
  const fake = new FakeElm(withReply("221741", ["7F2278", "6217411426"]));
  assert.deepEqual(await new Uds(await Elm.open(fake)).readDid(0x1741), h("1426"));
});

test("the pending line reaches the client before the final answer", async () => {
  const fake = new FakeElm(withReply("221741", ["7F2278", "6217411426", "HOLD"]));
  const elm = await Elm.open(fake);
  await elm.send(h("221741"));
  assert.deepEqual(await elm.recv(500), h("7f2278"));
  fake.release();
  assert.deepEqual(await elm.recv(500), h("6217411426"));
});

test("every pending restarts the wait: an answer after P2 but within P2* arrives", async () => {
  const fake = new FakeElm(withReply("221741", ["7F2278", "6217411426", "HOLD"]));
  const uds = new Uds(await Elm.open(fake), { p2: 50, p2Star: 1000 });
  const reading = uds.readDid(0x1741);
  setTimeout(() => fake.release(), 150);
  assert.deepEqual(await reading, h("1426"));
});

test("without a pending, an answer after P2 is a timeout", async () => {
  const fake = new FakeElm(withReply("221741", ["6217411426", "HOLD"]));
  const uds = new Uds(await Elm.open(fake), { p2: 50 });
  await assert.rejects(uds.readDid(0x1741), { name: "TimeoutError", message: "no response to 221741" });
});

test("a request after 1.5 s without traffic is preceded by 3E 00", async () => {
  let now = 0;
  const fake = new FakeElm(withReply("3e00", ["7E00"]));
  const uds = new Uds(await Elm.open(fake), { clock: () => now });
  await uds.readDid(0x1b68);
  now = 2000;
  await uds.readDid(0x1b69);
  assert.deepEqual(fake.requests, ["221b68", "3e00", "221b69"]);
});

test("a multi-frame answer is reassembled, with or without spaces", async () => {
  const spaced = ["00E", "0: 62 F1 87 39 39 31", "1: 36 31 38 36 39 32 31", "2: 37 AA AA AA AA AA AA"];
  for (const lines of [spaced, didAnswer(0xf187, enc("99161869217"))]) {
    const uds = new Uds(await Elm.open(new FakeElm(withReply("22f187", lines))));
    assert.equal(dec(await uds.readDid(0xf187)), "99161869217");
  }
});

test("a multi-frame answer with a missing frame or cut short is refused", async () => {
  const lines = didAnswer(0xf187, enc("99161869217"));
  for (const broken of [[lines[0], lines[1], lines[3]], lines.slice(0, 3)]) {
    const elm = await Elm.open(new FakeElm(withReply("22f187", broken)));
    await elm.send(h("22f187"));
    await assert.rejects(elm.recv(500), TransportError);
  }
});

test("NO DATA is a timeout and the next request still works", async () => {
  const uds = new Uds(await Elm.open(new FakeElm(withReply("221b68", ["NO DATA"]))));
  await assert.rejects(uds.readDid(0x1b68), TimeoutError);
  assert.deepEqual(await uds.readDid(0x1b69), h("046f"));
});

test("a prompt without an answer is a timeout", async () => {
  const elm = await Elm.open(new FakeElm(withReply("221b68", [])));
  await elm.send(h("221b68"));
  await assert.rejects(elm.recv(500), TimeoutError);
});

test("an adapter error line is a transport error", async () => {
  const elm = await Elm.open(new FakeElm(withReply("221b68", ["CAN ERROR"])));
  await elm.send(h("221b68"));
  await assert.rejects(elm.recv(500), { name: "TransportError", message: "adapter: CAN ERROR" });
});

test("a silent adapter times out, and the one outstanding read is reused afterwards", async () => {
  const fake = new FakeElm(withReply("221b68", ["621B680D36", "HOLD"]));
  const elm = await Elm.open(fake);
  await elm.send(h("221b68"));
  await assert.rejects(elm.recv(20), TimeoutError);
  await assert.rejects(elm.recv(20), TimeoutError);  // a second read() here would trip the fake
  fake.release();
  assert.deepEqual(await elm.recv(500), h("621b680d36"));
});

test("bare \\r line ends work as well as \\r\\n", async () => {
  const fake = new FakeElm(car(), { linefeeds: false });
  const { result, error } = await read(fake);
  assert.equal(error, undefined);
  assert.deepEqual(result.ranges.map(r => r.ignitions), [3382, 1135, 664, 272, 166, 104]);
});

// --- readCar ---------------------------------------------------------------------

test("readCar reads the counters, the peak record and the fault codes like the CLI", async () => {
  const fake = new FakeElm(car());
  const { result, error, steps } = await read(fake);
  assert.equal(error, undefined);
  assert.deepEqual(steps, [["adapter", "ok"], ["car", "ok"], ["read", "ok"]]);
  assert.deepEqual(result.ranges.map(r => [r.range, r.ignitions, r.time_min]), [
    [1, 3382, 30948], [2, 1135, 29928], [3, 664, 27252], [4, 272, 27252], [5, 166, 27252], [6, 104, 6312],
  ]);
  assert.deepEqual(result.ranges.map(r => r.hours.toFixed(1)), ["515.8", "498.8", "454.2", "454.2", "454.2", "105.2"]);
  assert.deepEqual([result.peak.max_rpm, result.peak.over_speed_s], [11000, 4.6]);
  assert.equal(result.peak.describe(), "Peak engine speed: 11000 rpm. Time above the over-speed limit: 4.6 s.");
  assert.deepEqual(result.codes.map(c => [c.code, describeStatus(c.status)]), [["00ABCD", "stored (failed since last clear)"]]);
  assert.equal(result.identity.describe(), "DME 991.618.692.17, software 3671");
  assert.ok(result.identity.tested);
  assert.equal(untestedNote(result.identity), null);
  assert.deepEqual(fake.requests, EVERY_REQUEST);
  assert.ok(fake.closed);
});

test("readCar never reads the VIN", async () => {
  const fake = new FakeElm(car());
  await read(fake);
  assert.ok(!fake.requests.includes("22f190"));
});

test("an untested DME is read and flagged with the note and the issue link", async () => {
  const { result } = await read(new FakeElm(car({ dme: [null, "3671"] })));
  assert.equal(result.identity.describe(), "DME part number unknown, software 3671");
  assert.ok(!result.identity.tested);
  assert.equal(untestedNote(result.identity),
    "This DME (part number unknown, software 3671) has not been tested with overrev, so the values may not mean "
    + "what the labels say. Please share the output of 'overrev probe': "
    + "https://github.com/YannKr/porsche-overrev/issues/new?template=new-car.yml");
  assert.ok(untestedNote(result.identity).endsWith(ISSUE_LINK));
  assert.equal(result.ranges.length, 6);
});

test("car text goes into the untested note literally, even with $ patterns", () => {
  const identity = new Identity("99161869299", "A$'B$`C$&D$$E");
  assert.equal(untestedNote(identity),
    "This DME (991.618.692.99, software A$'B$`C$&D$$E) has not been tested with overrev, so the values may not mean "
    + "what the labels say. Please share the output of 'overrev probe': "
    + "https://github.com/YannKr/porsche-overrev/issues/new?template=new-car.yml");
  assert.equal(untestedNote(identity).split(ISSUE_LINK).length, 2);
});

test("a DME without the peak record or the fault memory still reads", async () => {
  const respond = withReply("221b8a", ["7F2231"], withReply("19022c", ["7F1911"]));
  const { result, error } = await read(new FakeElm(respond));
  assert.equal(error, undefined);
  assert.equal(result.peak, null);
  assert.equal(result.codes, null);
  assert.equal(result.codesNote, "negative response to 19022c: NRC 0x11 serviceNotSupported");
});

const FAILURES = [
  ["a sleeping car says turn the ignition on", "1003", ["CAN ERROR"], "car", HINT_IGNITION, "adapter: CAN ERROR"],
  ["no answer to the session says turn the ignition on", "1003", ["NO DATA"], "car", HINT_IGNITION, "no response to 1003"],
  ["a refused session names the tested DME", "1003", ["7F1012"], "car", HINT_SESSION_REFUSED,
    "negative response to 1003: NRC 0x12 subFunctionNotSupported"],
  ["a refused counter gives the SecurityAccess hint", "221b68", ["7F2233"], "read", SECURITY_HINT,
    "negative response to 221b68: NRC 0x33 securityAccessDenied"],
  ["a refused counter out of range gives no hint", "221b68", ["7F2231"], "read", null,
    "negative response to 221b68: NRC 0x31 requestOutOfRange"],
  ["the car going quiet mid-read says keep the ignition on", "221b69", ["NO DATA"], "read", HINT_READ_INTERRUPTED,
    "no response to 221b69"],
];

for (const [name, request, lines, key, hint, message] of FAILURES) {
  test(name, async () => {
    const fake = new FakeElm(withReply(request, lines));
    const { error, steps } = await read(fake);
    const keys = ["adapter", "car", "read"];
    assert.deepEqual(steps.slice(0, -1), keys.slice(0, keys.indexOf(key)).map(k => [k, "ok"]));
    assert.deepEqual(steps.at(-1), [key, "fail", error, hint]);
    assert.equal(error.message, message);
    assert.ok(fake.closed);
  });
}

test("a silent adapter fails the adapter step with the unplug hint", async () => {
  const { error, steps } = await read(new FakeElm(car(), { swallow: 99 }), FAST);
  assert.ok(error instanceof TimeoutError);
  assert.deepEqual(steps, [["adapter", "fail", error, HINT_ADAPTER_SILENT]]);
});

test("a NegativeResponse carries the request and the NRC", async () => {
  const { error } = await read(new FakeElm(withReply("221b68", ["7F2233"])));
  assert.ok(error instanceof NegativeResponse && error instanceof ProtocolError);
  assert.deepEqual([hexOf(error.request), error.nrc], ["221b68", 0x33]);
});

// --- decoding --------------------------------------------------------------------

test("fault codes are three-byte DTCs with a status byte", () => {
  const codes = parseFaultCodes(h("5902ff" + "00123409" + "00abcd60"));
  assert.deepEqual(codes.map(c => [c.code, c.status]), [["001234", 0x09], ["00ABCD", 0x60]]);
  assert.deepEqual(codes.map(c => describeStatus(c.status)), ["active, confirmed", "stored (failed since last clear)"]);
  assert.deepEqual(parseFaultCodes(h("5902ff")), []);
  assert.equal(describeStatus(0x10), "status 0x10");
  assert.equal(describeStatus(0x04), "pending");
  for (const raw of ["5902", "5902ff001234", "5902ff0012340900"]) assert.throws(() => parseFaultCodes(h(raw)), ProtocolError);
});

test("text fields: ASCII as is, padding stripped, nothing as null, anything else as hex", () => {
  const cases = [
    [enc("3671\0 "), "3671"], [enc("\0\0 99161869217 "), "99161869217"], [enc("ECM40FS"), "ECM40FS"],
    [enc(""), null], [enc("\0\0  "), null], [h("01ff"), "01FF"], [enc("AB\0CD"), "4142004344"],
  ];
  for (const [raw, text] of cases) assert.equal(textField(raw), text);
});

test("part numbers are shown with dots only when they are 11 digits", () => {
  const cases = [["99161869217", "991.618.692.17"], ["9916186921", "9916186921"], ["991618692170", "991618692170"],
    ["9916186921A", "9916186921A"], ["01FF", "01FF"]];
  for (const [part, shown] of cases) assert.equal(formatPartNumber(part), shown);
});

test("only the GT3 DME is tested", () => {
  assert.deepEqual(TESTED_DMES, [["99161869217", "3671"]]);
  assert.ok(new Identity("99161869217", "3671").tested);
  for (const [part, software] of [["99161869217", "3672"], ["99161869299", "3671"], [null, "3671"],
    ["99161869217", null], [null, null]]) {
    assert.ok(!new Identity(part, software).tested);
  }
  assert.equal(new Identity("99161869299", null).describe(), "DME 991.618.692.99, software unknown");
  assert.equal(new Identity(null, null).describe(), "DME part number unknown, software unknown");
});

test("the peak engine speed is half an rpm per bit", async () => {
  const { result } = await read(new FakeElm(car({ peakRaw: [0x55f1, 0x0016] })));
  assert.deepEqual([result.peak.max_rpm, result.peak.over_speed_s], [11000.5, 4.4]);
  assert.equal(result.peak.describe(), "Peak engine speed: 11000.5 rpm. Time above the over-speed limit: 4.4 s.");
});

// --- the same words as the CLI ---------------------------------------------------

const PYTHON = existsSync(`${ROOT}.venv/bin/python`) ? `${ROOT}.venv/bin/python` : "python3";

test("the page uses the CLI's texts word for word", () => {
  const code = [
    "import json",
    "from overrev import decode, steps",
    "print(json.dumps({'TEXT': steps.TEXT, 'HINTS': [steps.HINT_ADAPTER_SILENT, steps.HINT_IGNITION,",
    "  steps.HINT_SESSION_REFUSED, steps.SECURITY_HINT, steps.HINT_READ_INTERRUPTED],",
    "  'UNTESTED_NOTE': decode.UNTESTED_NOTE, 'TABLE_EXPLANATION': decode.TABLE_EXPLANATION,",
    "  'ISSUE_LINK': decode.ISSUE_LINK, 'TESTED_DMES': sorted(decode.TESTED_DMES)}))",
  ].join("\n");
  const python = JSON.parse(execFileSync(PYTHON, ["-c", code], { env: { ...process.env, PYTHONPATH: `${ROOT}src` } }));
  assert.deepEqual(TEXT, python.TEXT);
  assert.deepEqual([HINT_ADAPTER_SILENT, HINT_IGNITION, HINT_SESSION_REFUSED, SECURITY_HINT, HINT_READ_INTERRUPTED],
    python.HINTS);
  assert.equal(UNTESTED_NOTE, python.UNTESTED_NOTE);
  assert.equal(TABLE_EXPLANATION, python.TABLE_EXPLANATION);
  assert.equal(ISSUE_LINK, python.ISSUE_LINK);
  assert.deepEqual(TESTED_DMES, python.TESTED_DMES);
});

// --- the page: connect, clean up, render -----------------------------------------

class FakePort {
  // A Web Serial port whose device is a new FakeElm at every open().
  constructor(devices, log, { openError = null, cancelError = null } = {}) {
    this.devices = devices;
    this.log = log;
    this.openError = openError;
    this.cancelError = cancelError;
    this.isOpen = false;
  }

  async open({ baudRate }) {
    this.log.push(`open ${baudRate}`);
    if (this.isOpen) throw new DOMException("The port is already open.", "InvalidStateError");
    if (this.openError) throw this.openError;
    this.isOpen = true;
    const device = this.devices.shift();
    const { log, cancelError } = this;
    this.readable = {
      getReader: () => ({
        read: async () => {
          const value = await device.read();
          return value === null ? { done: true, value: undefined } : { done: false, value };
        },
        cancel: async () => {
          log.push("reader.cancel");
          device.hangUp();
          if (cancelError) throw cancelError;
        },
        releaseLock: () => log.push("reader.releaseLock"),
      }),
    };
    this.writable = {
      getWriter: () => ({ write: data => device.write(data), releaseLock: () => log.push("writer.releaseLock") }),
    };
  }

  async close() {
    this.log.push("port.close");
    if (!this.isOpen) throw new DOMException("The port is already closed.", "InvalidStateError");
    this.isOpen = false;
  }
}

function page(port, { pickerError = null } = {}) {
  const calls = [];
  const asked = [];
  const serial = {
    requestPort: async options => {
      asked.push(options);
      if (pickerError) throw pickerError;
      return port;
    },
  };
  const view = {
    reset: () => calls.push(["reset"]),
    message: text => calls.push(["message", text]),
    step: (key, outcome, error, hint) => calls.push(["step", key, outcome, error?.message, hint]),
    results: result => calls.push(["results", result.ranges.map(r => r.ignitions)]),
  };
  return { serial, view, calls, asked };
}

const CLEANUP = ["reader.cancel", "reader.releaseLock", "writer.releaseLock", "port.close"];

test("Connect asks for a Bluetooth serial port, reads, shows the results and closes the port", async () => {
  const log = [];
  const { serial, view, calls, asked } = page(new FakePort([new FakeElm(car())], log));
  await connect(serial, view);
  assert.deepEqual(asked, [{ allowedBluetoothServiceClassIds: ["00001101-0000-1000-8000-00805f9b34fb"] }]);
  assert.deepEqual(log, ["open 115200", ...CLEANUP]);
  assert.deepEqual(calls, [
    ["reset"],
    ["step", "adapter", "ok", undefined, undefined],
    ["step", "car", "ok", undefined, undefined],
    ["step", "read", "ok", undefined, undefined],
    ["results", [3382, 1135, 664, 272, 166, 104]],
  ]);
});

test("a cancelled picker says only that no adapter was chosen", async () => {
  const log = [];
  const picker = new DOMException("No port selected by the user.", "NotFoundError");
  const { serial, view, calls } = page(new FakePort([], log), { pickerError: picker });
  await connect(serial, view);
  assert.deepEqual(calls, [["reset"], ["message", "No adapter chosen."]]);
  assert.deepEqual(log, []);
});

test("an error mid-read is shown once by its step, the port is closed, and a second click works", async () => {
  const log = [];
  const port = new FakePort([new FakeElm(withReply("221b69", ["NO DATA"])), new FakeElm(car())], log);
  const first = page(port);
  await connect(first.serial, first.view);
  assert.deepEqual(first.calls.at(-1), ["step", "read", "fail", "no response to 221b69", HINT_READ_INTERRUPTED]);
  assert.ok(!first.calls.some(([kind]) => kind === "message"));
  assert.deepEqual(log, ["open 115200", ...CLEANUP]);
  const second = page(port);
  await connect(second.serial, second.view);
  assert.deepEqual(second.calls.at(-1), ["results", [3382, 1135, 664, 272, 166, 104]]);
  assert.deepEqual(log, ["open 115200", ...CLEANUP, "open 115200", ...CLEANUP]);
});

test("a port that cannot be opened fails the adapter step with the error, then what to check", async () => {
  const log = [];
  const refused = new DOMException("Failed to open serial port.", "NetworkError");
  const { serial, view, calls } = page(new FakePort([], log, { openError: refused }), {});
  await connect(serial, view);
  const next = "Check that the adapter is plugged in with the ignition on, and that no phone or other program is "
    + "connected to it, then try again.";
  assert.deepEqual(calls, [["reset"], ["step", "adapter", "fail", "Failed to open serial port.", next]]);
  assert.deepEqual(log, ["open 115200", "port.close"]);
});

test("every clean-up step runs even when an earlier one fails", async () => {
  const log = [];
  const port = new FakePort([new FakeElm(car()), new FakeElm(car())], log, { cancelError: new TypeError("gone") });
  const { serial, view } = page(port);
  await connect(serial, view);
  assert.deepEqual(log, ["open 115200", ...CLEANUP]);
  assert.ok(!port.isOpen);
});

test("the page sets car data as text, never as HTML, and loads nothing from another origin", () => {
  for (const file of ["docs/app.js", "docs/overrev.js"]) {
    const source = readFileSync(`${ROOT}${file}`, "utf8");
    assert.doesNotMatch(source, /innerHTML|outerHTML|insertAdjacentHTML|document\.write|eval\(|new Function/, file);
  }
  const html = readFileSync(`${ROOT}docs/index.html`, "utf8");
  assert.match(html, /<title>overrev: DME over-rev reader<\/title>/);
  assert.match(html, /<meta http-equiv="Content-Security-Policy" content="default-src 'self'; style-src 'self' 'unsafe-inline'">/);
  assert.match(html, /<script type="module" src="app.js"><\/script>/);
  const loaded = [...html.matchAll(/<(?:script|link|img|iframe)\b[^>]*?\b(?:src|href)="([^"]*)"/g)].map(m => m[1]);
  assert.deepEqual(loaded, ["app.js"]);
  assert.doesNotMatch(html, /@import|url\(/);
});

test("the landing page offers the PID file for download, a byte copy of the one in deploy/obdlink", () => {
  const html = readFileSync(`${ROOT}docs/index.html`, "utf8");
  assert.match(html, /<a [^>]*href="overrev-pids\.udpid" download[ >]/);
  assert.equal(readFileSync(`${ROOT}docs/overrev-pids.udpid`, "utf8"),
    readFileSync(`${ROOT}deploy/obdlink/overrev-pids.udpid`, "utf8"));
});

test("the landing page's install lines are the one-liner get.sh documents, plain and with --auto", () => {
  const html = readFileSync(`${ROOT}docs/index.html`, "utf8");
  const oneLine = readFileSync(`${ROOT}get.sh`, "utf8").match(/^#\s+(curl -fsSL \S+ \| sh)$/m)[1];
  const commands = [...html.matchAll(/<code class="cmd">([^<]*)<\/code>/g)].map(m => m[1]);
  assert.deepEqual(commands, [oneLine, `${oneLine} -s -- --auto`]);
});

function copyPage(clipboard) {
  const label = { textContent: "Copy" };
  const code = { textContent: "curl x | sh" };
  const selected = [];
  let onClick;
  const button = { previousElementSibling: code, querySelector: () => label, addEventListener: (type, fn) => { onClick = fn; } };
  const doc = { querySelectorAll: () => [button], getSelection: () => ({ selectAllChildren: node => selected.push(node) }) };
  copyButtons(doc, clipboard ? { clipboard } : {});
  return { click: () => onClick(), label, code, selected };
}

test("a copy button puts its command on the clipboard and says so", async () => {
  const copied = [];
  const page = copyPage({ writeText: async text => { copied.push(text); } });
  await page.click();
  assert.deepEqual(copied, ["curl x | sh"]);
  assert.equal(page.label.textContent, "Copied");
});

test("without clipboard access, a copy button selects the command for the user to copy", async () => {
  const page = copyPage(null);
  await page.click();
  assert.deepEqual(page.selected, [page.code]);
  assert.equal(page.label.textContent, "Selected: copy it");
});
