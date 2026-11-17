"""Write the run report: report.json (machine readable) and report.html (evidence for sign-off)."""
from __future__ import annotations

import datetime as _dt
import html
import json
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from . import __version__
from .config import Config


def _plain(o: Any) -> Any:
    if is_dataclass(o):
        d = asdict(o)
        for prop in ("ok", "rows"):
            if hasattr(type(o), prop) and isinstance(getattr(type(o), prop), property):
                d[prop] = getattr(o, prop)
        return d
    return str(o)


def write(cfg: Config, run: Any = None, validation: Any = None, estimate: Any = None,
          out_dir: Optional[str] = None) -> Dict[str, str]:
    """Write ``report.json`` and ``report.html`` to ``out_dir`` (default ``cfg.report_dir``).

    Pass any of a :class:`RunResult`, a ``ValidationResult`` or an :class:`Estimate`.
    Passwords are redacted. Returns ``{"json": path, "html": path}``.
    """
    out = Path(out_dir or cfg.report_dir)
    out.mkdir(parents=True, exist_ok=True)
    validation = validation or (run.validation if run is not None else None)
    data = {
        "tool": f"mssql-ocp-migrate {__version__}",
        "generated": _dt.datetime.now().isoformat(timespec="seconds"),
        "config": cfg.to_dict(redact=True),
        "run": _plain(run) if run is not None else None,
        "validation": _plain(validation) if validation is not None else None,
        "validation_summary": validation.summary() if validation is not None else None,
        "estimate": estimate.to_dict() if estimate is not None else None,
    }
    if run is not None:
        data["run_ok"] = run.ok
    jpath = out / "report.json"
    jpath.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    hpath = out / "report.html"
    hpath.write_text(_html(cfg, run, validation, estimate), encoding="utf-8")
    return {"json": str(jpath), "html": str(hpath)}


def _e(x: Any) -> str:
    return html.escape(str(x))


def _html(cfg: Config, run, validation, est) -> str:
    ok = (run.ok if run is not None else True) and (validation.ok if validation is not None else True)
    rows = []
    status_line = "PASSED" if ok else "FAILED"
    parts = [f"""<!doctype html><html><head><meta charset="utf-8">
<title>SQL Server migration report</title>
<style>
body{{font:14px/1.45 system-ui,-apple-system,Segoe UI,sans-serif;margin:32px;color:#1d1d1f;max-width:1200px}}
h1{{font-size:22px;margin:0 0 4px}} h2{{font-size:17px;margin:28px 0 8px}}
.badge{{display:inline-block;padding:3px 10px;border-radius:12px;font-weight:600;color:#fff;
background:{'#1a7f37' if ok else '#cf222e'}}}
table{{border-collapse:collapse;width:100%;margin:6px 0}} th,td{{border-bottom:1px solid #e5e5e5;
padding:6px 8px;text-align:left;vertical-align:top}} th{{background:#f6f8fa;font-weight:600}}
td.n{{text-align:right;font-variant-numeric:tabular-nums}} .bad{{color:#cf222e;font-weight:600}}
.ok{{color:#1a7f37}} .muted{{color:#6e7781}} code{{background:#f6f8fa;padding:1px 4px;border-radius:4px}}
</style></head><body>
<h1>SQL Server &rarr; OpenShift migration report</h1>
<div class="muted">{_e(cfg.source.describe())} &rarr; {_e(cfg.target.describe())} &middot;
generated {_e(_dt.datetime.now().isoformat(timespec='seconds'))}</div>
<p><span class="badge">{status_line}</span></p>"""]
    if run is not None:
        dur = (run.finished or run.started) - run.started
        parts.append(f"<h2>Copy</h2><p>{run.rows:,} rows in {dur / 60:,.1f} min "
                     f"({run.rows / max(dur, 1e-6):,.0f} rows/s overall). "
                     f"Source {_e(run.source_info.get('version', ''))} {_e(run.source_info.get('edition', ''))}; "
                     f"target {_e(run.target_info.get('version', ''))} {_e(run.target_info.get('edition', ''))}.</p>")
        if run.identity_strategy:
            parts.append("<p class='muted'>Identity load strategy: "
                         + ", ".join(f"<code>{_e(v)}</code>" for v in run.identity_strategy.values()) + "</p>")
    if validation is not None:
        s = validation.summary()
        parts.append(f"<h2>Validation ({_e(s['mode'])})</h2><p>{s['tables'] - s['tables_failed']}/{s['tables']} "
                     f"tables passed &middot; {s['rows_source']:,} source rows vs {s['rows_target']:,} target rows "
                     f"&middot; {s['chunks_checksummed']} chunks checksummed, {s['chunks_row_compared']} "
                     f"compared row by row (SHA-256) &middot; {s['seconds']:,} s</p>")
    parts.append("<table><tr><th>Table</th><th class=n>Source rows</th><th class=n>Target rows</th>"
                 "<th class=n>Chunks</th><th class=n>Copy s</th><th>Result</th></tr>")
    names = sorted(set((run.tables if run is not None else {}).keys())
                   | set((validation.tables if validation is not None else {}).keys()))
    for n in names:
        tr = run.tables.get(n) if run is not None else None
        tc = validation.tables.get(n) if validation is not None else None
        problems = []
        if tr is not None and tr.failed_chunks:
            problems.append(f"{len(tr.failed_chunks)} chunk(s) failed to copy: {tr.errors[0][:200]}")
        if tc is not None:
            if tc.source_rows != tc.target_rows:
                problems.append("row count differs")
            problems.extend(tc.schema_diffs[:5])
            for c in tc.chunks:
                if not c.ok:
                    detail = c.error or "; ".join(filter(None, [
                        c.missing_in_target and f"missing keys {', '.join(c.missing_in_target[:5])}",
                        c.extra_in_target and f"extra keys {', '.join(c.extra_in_target[:5])}",
                        c.different and f"changed rows {', '.join(c.different[:5])}"]))
                    problems.append(f"{c.chunk_id}: {detail}")
        result = ("<span class=bad>" + "<br>".join(_e(p) for p in problems[:12]) + "</span>") if problems \
            else "<span class=ok>match</span>"
        rows.append(f"<tr><td>{_e(n)}</td><td class=n>{_fmt(tc.source_rows if tc else None)}</td>"
                    f"<td class=n>{_fmt(tc.target_rows if tc else (tr.rows if tr else None))}</td>"
                    f"<td class=n>{_fmt(tr.chunks if tr else (len(tc.chunks) if tc else None))}</td>"
                    f"<td class=n>{f'{tr.seconds:,.1f}' if tr else ''}</td><td>{result}</td></tr>")
    parts.append("".join(rows) + "</table>")
    if validation is not None and validation.object_diffs:
        parts.append("<h2>Object differences</h2><ul>" +
                     "".join(f"<li class=bad>{_e(d)}</li>" for d in validation.object_diffs) + "</ul>")
    if run is not None and run.ddl_errors:
        parts.append("<h2>DDL errors</h2><table><tr><th>Object</th><th>Error</th></tr>" +
                     "".join(f"<tr><td>{_e(d['object'])}</td><td>{_e(d['error'])}</td></tr>"
                             for d in run.ddl_errors) + "</table>")
    if run is not None and getattr(run, "assessment", None) is not None and run.assessment.findings:
        parts.append("<h2>Readiness assessment</h2><table><tr><th>Severity</th><th>Area</th><th>Item</th>"
                     "<th>Finding</th><th>What to do</th></tr>" +
                     "".join(f"<tr><td class={'bad' if f.severity == 'blocker' else ''}>{_e(f.severity)}</td>"
                             f"<td>{_e(f.area)}</td><td>{_e(f.item)}</td><td>{_e(f.message)}</td><td>{_e(f.fix)}</td></tr>"
                             for f in run.assessment.findings) + "</table>")
    if run is not None and run.warnings:
        parts.append("<h2>Warnings</h2><ul>" + "".join(f"<li>{_e(w)}</li>" for w in run.warnings) + "</ul>")
    if est is not None:
        from .estimate import format_estimate
        parts.append(f"<h2>Estimate</h2><pre>{_e(format_estimate(est))}</pre>")
    parts.append("</body></html>")
    return "\n".join(parts)


def _fmt(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, int):
        return "missing" if v < 0 else f"{v:,}"
    return _e(v)
