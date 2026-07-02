"""
Healthcheck script – reads services from services.csv, checks each service
(HTTP status, response time, XPath element), stores JSON results under
docs/data/ and regenerates docs/index.html.  Sends an e-mail alert to the
configured MS Teams channel address when a service is unhealthy.
"""

import csv
import json
import os
import smtplib
import sys
import time
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import requests
from lxml import html as lxml_html

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).parent
SERVICES_CSV = REPO_ROOT / "services.csv"
DATA_DIR = REPO_ROOT / "docs" / "data"
INDEX_HTML = REPO_ROOT / "docs" / "index.html"
RETENTION_DAYS = 14

# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

def check_service(url: str, expected_status: int, max_response_seconds: float, xpath: str) -> dict:
    """Run a single health-check and return a result dict."""
    result = {
        "url": url,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "status": "ok",
        "http_status": None,
        "response_time_s": None,
        "xpath_found": None,
        "errors": [],
    }

    try:
        timeout = max_response_seconds + 5
        start = time.monotonic()
        response = requests.get(url, timeout=timeout, allow_redirects=True)
        elapsed = time.monotonic() - start

        result["http_status"] = response.status_code
        result["response_time_s"] = round(elapsed, 3)

        if response.status_code != expected_status:
            result["status"] = "error"
            result["errors"].append(
                f"HTTP status {response.status_code} (expected {expected_status})"
            )

        if elapsed > max_response_seconds:
            result["status"] = "error"
            result["errors"].append(
                f"Response time {elapsed:.2f}s exceeds limit of {max_response_seconds}s"
            )

        if xpath:
            try:
                tree = lxml_html.fromstring(response.content)
                elements = tree.xpath(xpath)
                result["xpath_found"] = bool(elements)
                if not elements:
                    result["status"] = "error"
                    result["errors"].append(f"XPath element not found: {xpath}")
            except Exception as xpath_err:
                result["status"] = "error"
                result["errors"].append(f"XPath evaluation error: {xpath_err}")

    except requests.Timeout:
        result["status"] = "error"
        result["errors"].append(f"Request timed out after {max_response_seconds + 5}s")
    except requests.ConnectionError as exc:
        result["status"] = "error"
        result["errors"].append(f"Connection error: {exc}")
    except requests.RequestException as exc:
        result["status"] = "error"
        result["errors"].append(f"Request error: {exc}")

    return result


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------

def send_email_notification(results: list[dict]) -> None:
    """Send an e-mail alert (to a MS Teams channel address) for failed checks."""
    teams_email = os.environ.get("TEAMS_EMAIL", "").strip()
    smtp_host = os.environ.get("SMTP_HOST", "smtp.gmail.com").strip()
    smtp_port = int(os.environ.get("SMTP_PORT", "587"))
    smtp_user = os.environ.get("SMTP_USER", "").strip()
    smtp_password = os.environ.get("SMTP_PASSWORD", "").strip()

    if not teams_email:
        print("⚠  TEAMS_EMAIL not set – skipping e-mail notification.")
        return
    if not smtp_user or not smtp_password:
        print("⚠  SMTP_USER / SMTP_PASSWORD not set – skipping e-mail notification.")
        return

    failed = [r for r in results if r["status"] != "ok"]
    if not failed:
        return

    subject = f"[Healthcheck] {len(failed)} service(s) FAILED"

    rows_html = ""
    rows_text = ""
    for r in failed:
        error_str = "; ".join(r["errors"])
        rows_html += (
            f"<tr>"
            f"<td>{r['url']}</td>"
            f"<td>{r.get('http_status', 'N/A')}</td>"
            f"<td>{r.get('response_time_s', 'N/A')}</td>"
            f"<td>{error_str}</td>"
            f"</tr>\n"
        )
        rows_text += f"  • {r['url']}\n    Errors: {error_str}\n"

    html_body = f"""<html><body>
<p>The following services failed their health check at {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}:</p>
<table border="1" cellpadding="4" cellspacing="0">
<tr><th>URL</th><th>HTTP Status</th><th>Response Time (s)</th><th>Errors</th></tr>
{rows_html}
</table>
<p>Please investigate immediately.</p>
</body></html>"""

    text_body = (
        f"The following services failed their health check at "
        f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}:\n\n"
        f"{rows_text}\nPlease investigate immediately."
    )

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = smtp_user
    msg["To"] = teams_email
    msg.attach(MIMEText(text_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    try:
        with smtplib.SMTP(smtp_host, smtp_port) as server:
            server.ehlo()
            server.starttls()
            server.login(smtp_user, smtp_password)
            server.send_message(msg)
        print(f"✉  Notification e-mail sent to {teams_email}")
    except Exception as exc:
        print(f"✗  Failed to send notification e-mail: {exc}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def save_results(results: list[dict]) -> Path:
    """Write current results to a timestamped JSON file and return its path."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out_path = DATA_DIR / f"results-{ts}.json"
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(results, fh, indent=2, ensure_ascii=False)
    print(f"✔  Results saved to {out_path}")
    return out_path


def cleanup_old_results() -> None:
    """Delete result files older than RETENTION_DAYS."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)
    for f in sorted(DATA_DIR.glob("results-*.json")):
        try:
            # filename pattern: results-YYYYMMDD-HHMMSS.json
            date_part = f.stem.split("results-")[1]  # YYYYMMDD-HHMMSS
            file_dt = datetime.strptime(date_part, "%Y%m%d-%H%M%S").replace(
                tzinfo=timezone.utc
            )
            if file_dt < cutoff:
                f.unlink()
                print(f"🗑  Removed old result file: {f.name}")
        except (IndexError, ValueError):
            pass  # skip files that don't match the naming pattern


def load_all_results() -> list[dict]:
    """Load all result files from the last RETENTION_DAYS days."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)
    all_results: list[dict] = []
    for f in sorted(DATA_DIR.glob("results-*.json")):
        try:
            date_part = f.stem.split("results-")[1]
            file_dt = datetime.strptime(date_part, "%Y%m%d-%H%M%S").replace(
                tzinfo=timezone.utc
            )
            if file_dt >= cutoff:
                with open(f, encoding="utf-8") as fh:
                    batch = json.load(fh)
                    for item in batch:
                        item["_file"] = f.name
                    all_results.extend(batch)
        except (IndexError, ValueError, json.JSONDecodeError):
            pass
    return all_results


# ---------------------------------------------------------------------------
# HTML report generation
# ---------------------------------------------------------------------------

_STATUS_ICON = {"ok": "✅", "error": "❌"}
_STATUS_CLASS = {"ok": "ok", "error": "error"}


def _fmt_ts(ts: str) -> str:
    try:
        dt = datetime.fromisoformat(ts)
        return dt.strftime("%Y-%m-%d %H:%M UTC")
    except ValueError:
        return ts


def generate_html_report(all_results: list[dict]) -> None:
    """Generate docs/index.html from the accumulated results."""

    # Group by URL
    services: dict[str, list[dict]] = {}
    for r in all_results:
        services.setdefault(r["url"], []).append(r)

    # Sort each service's entries newest-first
    for url in services:
        services[url].sort(key=lambda x: x["timestamp"], reverse=True)

    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # --- build per-service cards ---
    cards_html = ""
    for url, entries in services.items():
        latest = entries[0]
        card_class = _STATUS_CLASS.get(latest["status"], "error")
        icon = _STATUS_ICON.get(latest["status"], "❓")

        history_rows = ""
        for e in entries[:336]:  # at most 2 weeks of hourly data
            st_class = _STATUS_CLASS.get(e["status"], "error")
            err_cell = "; ".join(e.get("errors", [])) or "—"
            xpath_cell = (
                "✅" if e.get("xpath_found") else ("❌" if e.get("xpath_found") is False else "—")
            )
            history_rows += (
                f'<tr class="{st_class}">'
                f"<td>{_fmt_ts(e['timestamp'])}</td>"
                f"<td>{_STATUS_ICON.get(e['status'], '❓')}</td>"
                f"<td>{e.get('http_status') or 'N/A'}</td>"
                f"<td>{e.get('response_time_s') if e.get('response_time_s') is not None else 'N/A'}</td>"
                f"<td>{xpath_cell}</td>"
                f"<td>{err_cell}</td>"
                f"</tr>\n"
            )

        cards_html += f"""
<div class="card {card_class}">
  <h2>{icon} {url}</h2>
  <p class="latest-info">
    Last check: {_fmt_ts(latest['timestamp'])} &nbsp;|&nbsp;
    HTTP: {latest.get('http_status') or 'N/A'} &nbsp;|&nbsp;
    Response time: {latest.get('response_time_s') if latest.get('response_time_s') is not None else 'N/A'} s
  </p>
  <details>
    <summary>History (last {RETENTION_DAYS} days)</summary>
    <table>
      <thead>
        <tr>
          <th>Timestamp</th><th>Status</th><th>HTTP</th>
          <th>Response (s)</th><th>XPath</th><th>Errors</th>
        </tr>
      </thead>
      <tbody>
{history_rows}
      </tbody>
    </table>
  </details>
</div>
"""

    if not cards_html:
        cards_html = "<p>No results available yet. The first check will run shortly.</p>"

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<meta http-equiv="refresh" content="300"/>
<title>Application Healthcheck Dashboard</title>
<style>
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    background: #f4f6f9;
    color: #222;
    margin: 0;
    padding: 20px;
  }}
  h1 {{ text-align: center; color: #1a73e8; }}
  .generated {{ text-align: center; color: #777; margin-bottom: 24px; font-size: 0.9em; }}
  .card {{
    background: #fff;
    border-radius: 8px;
    box-shadow: 0 2px 6px rgba(0,0,0,.1);
    margin: 0 auto 20px;
    max-width: 1100px;
    padding: 20px 24px;
    border-left: 6px solid #ccc;
  }}
  .card.ok  {{ border-left-color: #34a853; }}
  .card.error {{ border-left-color: #ea4335; }}
  .card h2 {{ margin: 0 0 8px; font-size: 1.1em; word-break: break-all; }}
  .latest-info {{ color: #555; font-size: 0.9em; margin: 0 0 12px; }}
  details summary {{ cursor: pointer; color: #1a73e8; font-size: 0.9em; }}
  table {{ border-collapse: collapse; width: 100%; font-size: 0.85em; margin-top: 10px; }}
  th {{ background: #e8eaf6; text-align: left; padding: 6px 10px; }}
  td {{ padding: 5px 10px; border-bottom: 1px solid #eee; }}
  tr.ok  td {{ background: #f6fff8; }}
  tr.error td {{ background: #fff8f8; }}
</style>
</head>
<body>
<h1>🔍 Application Healthcheck Dashboard</h1>
<p class="generated">Generated: {generated_at} &nbsp;|&nbsp; Data retention: {RETENTION_DAYS} days &nbsp;|&nbsp; Checks run every hour</p>
{cards_html}
</body>
</html>"""

    INDEX_HTML.parent.mkdir(parents=True, exist_ok=True)
    with open(INDEX_HTML, "w", encoding="utf-8") as fh:
        fh.write(html)
    print(f"✔  HTML report written to {INDEX_HTML}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    if not SERVICES_CSV.exists():
        print(f"✗  services.csv not found at {SERVICES_CSV}", file=sys.stderr)
        return 1

    results: list[dict] = []

    with open(SERVICES_CSV, newline="", encoding="utf-8") as csvfile:
        reader = csv.DictReader(csvfile, delimiter=";")
        for row in reader:
            url = row.get("url", "").strip()
            if not url:
                continue
            try:
                expected_status = int(row.get("expected_status", "200").strip())
            except ValueError:
                expected_status = 200
            try:
                max_response_seconds = float(row.get("max_response_seconds", "6").strip())
            except ValueError:
                max_response_seconds = 6.0
            xpath = row.get("xpath", "").strip()

            print(f"Checking {url} …", end=" ", flush=True)
            result = check_service(url, expected_status, max_response_seconds, xpath)
            results.append(result)
            print(result["status"].upper(), result.get("errors") or "")

    if not results:
        print("No services found in services.csv", file=sys.stderr)
        return 1

    save_results(results)
    cleanup_old_results()

    all_results = load_all_results()
    generate_html_report(all_results)

    # Notify on failures
    failed = [r for r in results if r["status"] != "ok"]
    if failed:
        send_email_notification(results)
        print(f"⚠  {len(failed)} service(s) reported errors.")
        return 1

    print("✅ All services are healthy.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
