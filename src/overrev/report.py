"""The one-page HTML report of a read: to keep, print, or send to a buyer or a mechanic."""
from __future__ import annotations

from datetime import datetime
from html import escape
from pathlib import Path

from .decode import TABLE_EXPLANATION, FaultCode, Identity, PeakReading, RangeReading

REPO_URL = "https://github.com/YannKr/porsche-overrev"

# No script, no link, no external file: the page is one file that works offline and prints.
STYLE = """
:root { color-scheme: light dark; --bg: #fff; --fg: #1c1c1e; --muted: #5f5f66; --line: #d4d4d8; }
@media (prefers-color-scheme: dark) {
  :root { --bg: #131316; --fg: #ececf0; --muted: #a1a1aa; --line: #3a3a42; }
}
@media print {
  :root { --bg: #fff; --fg: #000; --muted: #333; --line: #888; }
}
body { margin: 0; background: var(--bg); color: var(--fg); font: 16px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }
main { max-width: 40rem; margin: 0 auto; padding: 24px 16px 48px; }
h1 { font-size: 1.6rem; margin: 0 0 8px; }
h2 { font-size: 1.1rem; margin: 28px 0 8px; }
p { margin: 4px 0; }
.note, footer { color: var(--muted); font-size: 0.9rem; }
table { border-collapse: collapse; width: 100%; margin: 8px 0; font-variant-numeric: tabular-nums; }
th, td { padding: 6px 10px; border-bottom: 1px solid var(--line); text-align: right; }
th { border-bottom-width: 2px; }
code { font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace; }
ul { margin: 4px 0; padding-left: 20px; }
footer { margin-top: 32px; padding-top: 12px; border-top: 1px solid var(--line); }
"""


def _p(text: str) -> str:
    return f"<p>{escape(text)}</p>"


def _table(readings: list[RangeReading]) -> str:
    rows = "\n".join(
        f"<tr><td>{r.range}</td><td>{r.ignitions}</td><td>{r.hours:.1f}</td></tr>" for r in readings
    )
    return (
        "<table>\n<thead><tr><th scope=\"col\">Range</th><th scope=\"col\">Ignitions</th>"
        "<th scope=\"col\">Op. h at last event</th></tr></thead>\n"
        f"<tbody>\n{rows}\n</tbody>\n</table>"
    )


def _fault_codes(codes: list[FaultCode] | None) -> str:
    if codes is None:
        return _p("Fault memory: not available.")
    if not codes:
        return _p("Stored fault codes: none.")
    items = "\n".join(f"<li><code>{escape(c.code)}</code> {escape(c.describe())}</li>" for c in codes)
    return f"<p>Stored fault codes ({len(codes)}):</p>\n<ul>\n{items}\n</ul>"


def render(*, when: datetime, vin: str | None, identity: Identity, readings: list[RangeReading],
           peak: PeakReading | None, codes: list[FaultCode] | None, version: str) -> str:
    """A complete HTML document. Everything that came from the car goes through ``escape``."""
    dme = f"{identity.describe()} ({'tested' if identity.tested else 'not tested'})"
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>DME over-rev report</title>
<style>{STYLE}</style>
</head>
<body>
<main>
<h1>DME over-rev report</h1>
{_p(when.isoformat(sep=" ", timespec="seconds"))}
{_p(f"VIN {vin}" if vin else "VIN unreadable")}
{_p(dme)}
<h2>Over-rev counters</h2>
{_table(readings)}
<p class="note">{TABLE_EXPLANATION}</p>
<h2>Other records</h2>
{_p(peak.describe() if peak else "Peak record: not available on this DME.")}
{_fault_codes(codes)}
<footer>
{_p(f"Read with overrev {version}, which sends only read requests and changes nothing in the car.")}
{_p(REPO_URL)}
</footer>
</main>
</body>
</html>
"""


def write(path: str | Path, html: str) -> Path:
    """Save the report and return its absolute path; a write failure raises OSError."""
    target = Path(path).absolute()
    target.write_text(html, encoding="utf-8")
    return target
