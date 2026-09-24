"""Standalone, escaped terminal reports rendered only from saved evidence."""
from html import escape
import hashlib
import json
from pathlib import Path


def render_report(path, summary, records):
    path = Path(path)
    snapshot = {"summary": summary, "sessions": records}
    payload = json.dumps(snapshot, indent=2, sort_keys=True)
    digest = hashlib.sha256(payload.encode()).hexdigest()
    # Content-addressed snapshots and report versions preserve previous packets.
    evidence = path.with_name(f"evidence-{digest}.json")
    evidence.write_text(payload)
    rows = ''.join('<tr><td>' + escape(str(h['issue_id'])) + '</td><td>Completed</td><td>'
                   + escape(h['commit'][:12]) + '</td></tr>' for h in summary['history'])
    usage = summary['usage']
    html = '''<!doctype html><html lang="en"><meta charset="utf-8">
<title>Batch execution report</title><style>body{font:16px system-ui;max-width:1000px;margin:40px auto;padding:20px}td,th{padding:10px;text-align:left;border-bottom:1px solid #ccc}pre{white-space:pre-wrap;overflow-wrap:anywhere}table{border-collapse:collapse}</style>
<h1>Batch execution report</h1>'''
    html += '<p>Status: <strong>' + escape(summary['outcome']) + '</strong></p>'
    html += '<p>' + escape(summary['scope']) + '</p>'
    if summary.get('error'):
        html += '<p>Blocker: ' + escape(summary['error']) + '</p>'
    html += '<h2>Completed issues</h2><table><tr><th>Issue</th><th>Result</th><th>Revision</th></tr>' + rows + '</table>'
    html += '<h2>Usage</h2><table><tr><th>Scope</th><th>Input tokens</th><th>Cached input</th><th>Output tokens</th></tr>'
    arms = {"Runner sessions": usage} if "totals" in usage else usage
    for name, values in arms.items():
        totals = values.get("usage", values).get("totals", {})
        html += '<tr><td>' + escape(name) + '</td>' + ''.join(
            '<td>' + (f"{totals[key]:,}" if isinstance(totals.get(key), int) else "unknown") + '</td>'
            for key in ("input_tokens", "cached_input_tokens", "output_tokens")) + '</tr>'
    html += '</table><p>Cached input is part of total input. Missing telemetry is unknown. These counters are not billed cost; outer launch/reporting is excluded unless separately imported.</p>' 
    html += '<h2>Reproducibility appendix</h2><p>Evidence SHA-256: ' + digest + '</p><pre>' + escape(payload) + '</pre></html>'
    version = path.with_name(f"report-{digest}.html")
    version.write_text(html)
    path.write_text(html)
    return {"report": str(version), "evidence": str(evidence), "evidence_sha256": digest,
            "report_sha256": hashlib.sha256(html.encode()).hexdigest()}
