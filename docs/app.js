// The page: pick the OBDLink, read the car with overrev.js, show the steps and the results.
// Everything that came from the car is set as text (textContent, text nodes), never parsed as HTML.
import { ADAPTER, ISSUE_LINK, TABLE_EXPLANATION, TEXT, describeStatus, readCar, untestedNote } from "./overrev.js";

// Bluetooth Serial Port Profile: lets the port picker list a paired Bluetooth OBDLink.
const SERIAL_PORT_PROFILE = "00001101-0000-1000-8000-00805f9b34fb";
// The next step when the chosen port cannot be opened, before anything reached the adapter.
const HINT_PORT = "Check that the adapter is plugged in with the ignition on, and that no phone or other program is "
  + "connected to it, then try again.";

export async function connect(serial, view) {
  // One click: choose the port, read, show; the port is always closed again so the next click works.
  view.reset();
  let port;
  try {
    port = await serial.requestPort({ allowedBluetoothServiceClassIds: [SERIAL_PORT_PROFILE] });
  } catch (error) {
    view.message(error.name === "NotFoundError" ? "No adapter chosen." : `error: ${error.message}`);
    return;
  }
  let reader;
  let writer;
  let reported = null;  // the error a step already showed
  const onStep = (key, outcome, error, hintText) => {
    if (outcome === "fail") reported = error;
    view.step(key, outcome, error, hintText);
  };
  try {
    try {
      await port.open({ baudRate: 115200 });
      reader = port.readable.getReader();
      writer = port.writable.getWriter();
    } catch (error) {
      onStep(ADAPTER, "fail", error, HINT_PORT);
      throw error;
    }
    const io = {
      write: data => writer.write(data),
      read: async () => {
        const { value, done } = await reader.read();
        return done ? null : value;
      },
    };
    view.results(await readCar(io, onStep));
  } catch (error) {
    if (error !== reported) view.message(`error: ${error.message}`);
  } finally {
    // A pending read must be cancelled before its lock can be released, and the locks before the port closes.
    try { await reader?.cancel(); } catch { /* already cancelled or broken */ }
    try { reader?.releaseLock(); } catch { /* already released */ }
    try { writer?.releaseLock(); } catch { /* already released */ }
    try { await port.close(); } catch { /* not open */ }
  }
}

function pageView(doc) {
  const byId = id => doc.getElementById(id);
  const make = (tag, text, className) => {
    const element = doc.createElement(tag);
    if (text !== undefined) element.textContent = text;
    if (className) element.className = className;
    return element;
  };
  const link = (href, text) => {
    const a = make("a", text);
    a.href = href;
    return a;
  };
  const results = byId("results");

  function table(ranges) {
    const head = make("tr");
    for (const title of ["Range", "Ignitions", "Op. h at last event"]) {
      const th = make("th", title);
      th.scope = "col";
      head.append(th);
    }
    const body = make("tbody");
    for (const r of ranges) {
      const row = make("tr");
      for (const cell of [r.range, r.ignitions, r.hours.toFixed(1)]) row.append(make("td", String(cell)));
      body.append(row);
    }
    const thead = make("thead");
    thead.append(head);
    const element = make("table");
    element.append(thead, body);
    return element;
  }

  function faultCodes(codes, codesNote) {
    if (codes === null) return [make("p", `Fault memory: not available (${codesNote}).`)];
    if (!codes.length) return [make("p", "Stored fault codes: none.")];
    const list = make("ul");
    for (const c of codes) {
      const item = make("li");
      item.append(make("code", c.code), doc.createTextNode(` ${describeStatus(c.status)}`));
      list.append(item);
    }
    return [make("p", `Stored fault codes (${codes.length}):`), list];
  }

  return {
    reset() {
      byId("message").replaceChildren();
      byId("steps").replaceChildren();
      results.replaceChildren();
      results.hidden = true;
    },
    message(text) {
      byId("message").textContent = text;
    },
    step(key, outcome, error, hintText) {
      const [okText, failText] = TEXT[key];
      const item = make("li", outcome === "ok" ? `✓ ${okText}` : `✗ ${failText}`, outcome);
      if (error) item.append(make("p", `error: ${error.message}`, "error"));
      if (hintText) item.append(make("p", `hint: ${hintText}`, "hint"));
      byId("steps").append(item);
    },
    results({ identity, ranges, peak, codes, codesNote }) {
      results.append(make("h2", "Results"));
      results.append(make("p", `${identity.describe()} (${identity.tested ? "tested" : "not tested"})`));
      const note = untestedNote(identity);
      if (note) {
        const paragraph = make("p", undefined, "warning");
        paragraph.append(doc.createTextNode(note.slice(0, note.lastIndexOf(ISSUE_LINK))), link(ISSUE_LINK, ISSUE_LINK));
        results.append(paragraph);
      }
      results.append(table(ranges), make("p", TABLE_EXPLANATION, "note"));
      results.append(make("p", peak ? peak.describe() : "Peak record: not available on this DME."));
      results.append(...faultCodes(codes, codesNote));
      results.hidden = false;
    },
    noSerial() {
      byId("read").hidden = true;
      byId("browser-go").replaceWith(make("p", "This browser cannot reach the adapter. Open this page in Chrome or Edge "
        + "on a computer, or use the phone app.", "warning"));
    },
  };
}

export function copyButtons(doc, nav) {
  // Each copy button copies the command just before it. Without clipboard access, it selects the command instead.
  for (const button of doc.querySelectorAll("button.copy")) {
    const code = button.previousElementSibling;
    const label = button.querySelector("span");
    button.addEventListener("click", async () => {
      try {
        await nav.clipboard.writeText(code.textContent);
        label.textContent = "Copied";
      } catch {
        doc.getSelection()?.selectAllChildren(code);
        label.textContent = "Selected: copy it";
      }
    });
  }
}

function start(doc, nav) {
  copyButtons(doc, nav);
  const view = pageView(doc);
  if (!("serial" in nav)) {
    view.noSerial();
    return;
  }
  const button = doc.getElementById("connect");
  button.addEventListener("click", async () => {
    button.disabled = true;  // one read at a time: a second open of the same port would fail
    try {
      await connect(nav.serial, view);
    } finally {
      button.disabled = false;
    }
  });
}

if (typeof document !== "undefined") start(document, navigator);
