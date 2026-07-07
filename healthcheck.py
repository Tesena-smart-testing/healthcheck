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
SUMMARY_HTML = REPO_ROOT / "docs" / "summary.html"
RETENTION_DAYS = 14
LOGO_URL = "https://qecompass.tesena.com/assets/logo-symbol-BBvPgfPc.png"


def is_truthy(value: str | None) -> bool:
    """Return True for common truthy environment variable values."""
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

def check_service(name: str, url: str, expected_status: int, max_response_seconds: float, xpath: str) -> dict:
    """Run a single health-check and return a result dict."""
    result = {
        "name": name,
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

def send_teams_webhook_notification(results: list[dict], test_mode: bool = False) -> None:
    """Send notification to MS Teams channel using an Adaptive Card via Power Automate webhook."""
    webhook_url = os.environ.get("TEAMS_WEBHOOK_URL", "").strip()
    if not webhook_url:
        print("⚠  TEAMS_WEBHOOK_URL not set – skipping Teams webhook notification.")
        return

    failed = [r for r in results if r["status"] != "ok"]
    if not failed and not test_mode:
        return

    timestamp = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
    listed_results = results if test_mode else failed
    mode_label = "TEST" if test_mode else "ALERT"
    color = "0078D4" if test_mode else "E81828"  # blue for test, red for alert

    # Build service items for the card
    service_facts = []
    for r in listed_results:
        errors = "; ".join(r.get("errors", [])) or ("Current status OK" if test_mode else "—")
        service_facts.append({
            "name": r.get('name', r['url']),
            "value": f"**Status:** {r.get('status', 'N/A')} | **HTTP:** {r.get('http_status', 'N/A')} | **Time:** {r.get('response_time_s', 'N/A')}s"
        })
        service_facts.append({
            "name": "URL",
            "value": r['url']
        })
        service_facts.append({
            "name": "Errors" if not test_mode else "Details",
            "value": errors
        })

    # Build Adaptive Card payload
    card_body = [
        {
            "type": "TextBlock",
            "text": f"[Healthcheck {mode_label}]",
            "weight": "bolder",
            "size": "large",
            "color": color
        },
        {
            "type": "TextBlock",
            "text": f"Time: {timestamp}",
            "spacing": "small",
            "isSubtle": True
        }
    ]

    # Add fact set
    if service_facts:
        card_body.append({
            "type": "FactSet",
            "facts": service_facts,
            "spacing": "medium"
        })

    if not test_mode:
        card_body.append({
            "type": "TextBlock",
            "text": "Please investigate immediately.",
            "spacing": "medium",
            "weight": "bolder"
        })

    payload = {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.4",
        "body": card_body
    }

    try:
        response = requests.post(webhook_url, json=payload, timeout=10)
        response.raise_for_status()
        print("✉  Teams webhook notification sent")
    except requests.RequestException as exc:
        print(f"✗  Failed to send Teams webhook notification: {exc}", file=sys.stderr)

def send_email_notification(results: list[dict], test_mode: bool = False) -> None:
    """Send an e-mail alert (to a MS Teams channel address) for failed checks or test runs."""
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
    if not failed and not test_mode:
        return

    timestamp = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')

    if test_mode:
        subject = f"[Healthcheck TEST] SMTP notification test ({len(results)} service(s) checked)"
        intro_html = (
            f"<p>This is a <strong>test notification</strong> sent at {timestamp}. "
            f"It verifies that SMTP delivery to the configured mailbox or MS Teams channel works.</p>"
        )
        intro_text = (
            f"This is a TEST notification sent at {timestamp}. "
            f"It verifies that SMTP delivery to the configured mailbox or MS Teams channel works.\n\n"
        )
        closing_html = "<p>No action is required unless delivery or login failed.</p>"
        closing_text = "No action is required unless delivery or login failed."
        listed_results = results
    else:
        subject = f"[Healthcheck] {len(failed)} service(s) FAILED"
        intro_html = f"<p>The following services failed their health check at {timestamp}:</p>"
        intro_text = f"The following services failed their health check at {timestamp}:\n\n"
        closing_html = "<p>Please investigate immediately.</p>"
        closing_text = "Please investigate immediately."
        listed_results = failed

    rows_html = ""
    rows_text = ""
    for r in listed_results:
        error_str = "; ".join(r.get("errors", [])) or ("Test run - current status OK" if test_mode else "—")
        rows_html += (
            f"<tr>"
            f"<td>{r.get('name', r['url'])}</td>"
            f"<td>{r['url']}</td>"
            f"<td>{r.get('http_status', 'N/A')}</td>"
            f"<td>{r.get('response_time_s', 'N/A')}</td>"
            f"<td>{r.get('status', 'N/A')}</td>"
            f"<td>{error_str}</td>"
            f"</tr>\n"
        )
        rows_text += (
            f"  • {r.get('name', r['url'])}\n"
            f"    URL: {r['url']}\n"
            f"    HTTP: {r.get('http_status', 'N/A')}\n"
            f"    Response time: {r.get('response_time_s', 'N/A')}\n"
            f"    Status: {r.get('status', 'N/A')}\n"
            f"    Errors: {error_str}\n"
        )

    html_body = f"""<html><body>
{intro_html}
<table border="1" cellpadding="4" cellspacing="0">
<tr><th>Service</th><th>URL</th><th>HTTP Status</th><th>Response Time (s)</th><th>Status</th><th>Errors</th></tr>
{rows_html}
</table>
{closing_html}
</body></html>"""

    text_body = intro_text + rows_text + "\n" + closing_text

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
        mode = "test" if test_mode else "alert"
        print(f"✉  Notification e-mail sent to {teams_email} ({mode})")
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
  <h2>{icon} {latest.get('name', latest['url'])}</h2>
  <p class="latest-info">
    <small><a href="{latest['url']}" target="_blank" rel="noopener noreferrer">{latest['url']}</a></small><br/>
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
  .title {{
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 10px;
    margin-bottom: 8px;
  }}
  .title-logo {{
    width: 72px;
    height: 72px;
    object-fit: contain;
  }}
  .title-text {{
    margin: 0;
    color: #1a73e8;
  }}
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
<div class="title">
  <img src="{LOGO_URL}" alt="Tesena logo" class="title-logo"/>
  <h1 class="title-text">Application Healthcheck Dashboard</h1>
</div>
<p class="generated">Generated: {generated_at} &nbsp;|&nbsp; Data retention: {RETENTION_DAYS} days &nbsp;|&nbsp; <a href="https://github.com/Tesena-smart-testing/healthcheck/actions/workflows/healthcheck.yml" target="_blank" rel="noopener noreferrer">Checks run every hour</a></p>
{cards_html}
</body>
</html>"""

    INDEX_HTML.parent.mkdir(parents=True, exist_ok=True)
    with open(INDEX_HTML, "w", encoding="utf-8") as fh:
        fh.write(html)
    print(f"✔  HTML report written to {INDEX_HTML}")


def generate_summary_html(results: list[dict]) -> None:
    """Generate a concise summary page (docs/summary.html) with current check status."""
    
    # Group results by URL
    latest_by_url: dict[str, dict] = {}
    for r in results:
        if r["url"] not in latest_by_url:
            latest_by_url[r["url"]] = r
    
    # Count statuses
    ok_count = sum(1 for r in latest_by_url.values() if r["status"] == "ok")
    error_count = sum(1 for r in latest_by_url.values() if r["status"] != "ok")
    total_count = len(latest_by_url)
    
    # Overall status
    overall_status = "ok" if error_count == 0 else "error"
    status_emoji = "✅" if overall_status == "ok" else "❌"
    status_text = "All services operational" if overall_status == "ok" else f"{error_count} service(s) with issues"
    
    # Build service list HTML
    services_html = ""
    for url in sorted(latest_by_url.keys()):
        r = latest_by_url[url]
        icon = _STATUS_ICON.get(r["status"], "❓")
        http_status = r.get("http_status") or "N/A"
        response_time = r.get("response_time_s") or "N/A"
        errors = "; ".join(r.get("errors", [])) or "—"
        name = r.get("name", url)
        
        services_html += f"""  <tr class="{_STATUS_CLASS.get(r['status'], 'error')}">
    <td>{icon}</td>
    <td><strong>{name}</strong><br/><small style="color:#999;">{url}</small></td>
    <td>{http_status}</td>
    <td>{response_time}</td>
    <td>{errors}</td>
  </tr>
"""
    
    check_time = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<meta http-equiv="refresh" content="300"/>
<title>Healthcheck Summary</title>
<style>
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
    color: #222;
    margin: 0;
    padding: 20px;
    min-height: 100vh;
  }}
  .container {{
    max-width: 800px;
    margin: 0 auto;
    background: white;
    border-radius: 12px;
    box-shadow: 0 8px 24px rgba(0,0,0,.15);
    padding: 40px;
  }}
  .header {{
    text-align: center;
    margin-bottom: 32px;
  }}
  .status-badge {{
    font-size: 3em;
    margin-bottom: 16px;
  }}
  h1 {{
    color: #1a73e8;
    margin: 0 0 8px;
    font-size: 2em;
  }}
  .title {{
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 10px;
  }}
  .title-logo {{
    width: 72px;
    height: 72px;
    object-fit: contain;
  }}
  .status-text {{
    color: #555;
    font-size: 1.1em;
    margin: 8px 0 0 0;
  }}
  .stats {{
    display: flex;
    justify-content: space-around;
    background: #f5f5f5;
    border-radius: 8px;
    padding: 16px;
    margin-bottom: 24px;
    text-align: center;
  }}
  .stat {{
    flex: 1;
  }}
  .stat-value {{
    font-size: 1.8em;
    font-weight: bold;
    color: #1a73e8;
  }}
  .stat-label {{
    font-size: 0.9em;
    color: #999;
    margin-top: 4px;
  }}
  table {{
    width: 100%;
    border-collapse: collapse;
    margin-bottom: 16px;
  }}
  thead {{
    background: #e8eaf6;
    border-bottom: 2px solid #1a73e8;
  }}
  th {{
    padding: 12px;
    text-align: left;
    font-size: 0.9em;
    font-weight: 600;
  }}
  td {{
    padding: 10px 12px;
    border-bottom: 1px solid #eee;
  }}
  tr.ok {{
    background: #f0f9ff;
  }}
  tr.error {{
    background: #fff5f5;
  }}
  code {{
    background: #f5f5f5;
    padding: 2px 6px;
    border-radius: 3px;
    font-size: 0.9em;
  }}
  .footer {{
    text-align: center;
    color: #999;
    font-size: 0.85em;
    margin-top: 24px;
    padding-top: 16px;
    border-top: 1px solid #eee;
  }}
  a {{
    color: #1a73e8;
    text-decoration: none;
  }}
  a:hover {{
    text-decoration: underline;
  }}
</style>
</head>
<body>
<div class="container">
  <div class="header">
    <div class="status-badge">{status_emoji}</div>
    <div class="title">
      <img src="{LOGO_URL}" alt="Tesena logo" class="title-logo"/>
      <h1>Healthcheck Summary</h1>
    </div>
    <p class="status-text">{status_text}</p>
  </div>
  
  <div class="stats">
    <div class="stat">
      <div class="stat-value" style="color: #34a853;">{ok_count}</div>
      <div class="stat-label">Operational</div>
    </div>
    <div class="stat">
      <div class="stat-value" style="color: #ea4335;">{error_count}</div>
      <div class="stat-label">Issues</div>
    </div>
    <div class="stat">
      <div class="stat-value">{total_count}</div>
      <div class="stat-label">Total</div>
    </div>
  </div>
  
  <table>
    <thead>
      <tr>
        <th></th>
        <th>Service</th>
        <th>HTTP Status</th>
        <th>Response Time (s)</th>
        <th>Errors</th>
      </tr>
    </thead>
    <tbody>
{services_html}    </tbody>
  </table>
  
  <div class="footer">
    <p>Last check: {check_time}</p>
    <p><a href="index.html">→ View detailed report</a></p>
  </div>
</div>
</body>
</html>"""
    
    SUMMARY_HTML.parent.mkdir(parents=True, exist_ok=True)
    with open(SUMMARY_HTML, "w", encoding="utf-8") as fh:
        fh.write(html)
    print(f"✔  Summary page written to {SUMMARY_HTML}")



# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    if not SERVICES_CSV.exists():
        print(f"✗  services.csv not found at {SERVICES_CSV}", file=sys.stderr)
        return 1

    test_mode = is_truthy(os.environ.get("HEALTHCHECK_TEST_MODE"))
    has_webhook_notification = bool(os.environ.get("TEAMS_WEBHOOK_URL", "").strip())
    has_email_notification = bool(
      os.environ.get("TEAMS_EMAIL", "").strip()
      and os.environ.get("SMTP_USER", "").strip()
      and os.environ.get("SMTP_PASSWORD", "").strip()
    )

    if test_mode:
        print("ℹ  HEALTHCHECK_TEST_MODE enabled - a test notification will be sent after checks complete.")
    if not has_webhook_notification and not has_email_notification:
      print("⚠  No notification channel configured (set TEAMS_WEBHOOK_URL or SMTP + TEAMS_EMAIL).")

    results: list[dict] = []

    with open(SERVICES_CSV, newline="", encoding="utf-8") as csvfile:
        reader = csv.DictReader(csvfile, delimiter=";")
        for row in reader:
            name = row.get("name", "").strip()
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

            print(f"Checking {name or url} …", end=" ", flush=True)
            result = check_service(name, url, expected_status, max_response_seconds, xpath)
            results.append(result)
            print(result["status"].upper(), result.get("errors") or "")

    if not results:
        print("No services found in services.csv", file=sys.stderr)
        return 1

    save_results(results)
    cleanup_old_results()

    all_results = load_all_results()
    generate_html_report(all_results)
    generate_summary_html(results)

    failed = [r for r in results if r["status"] != "ok"]

    if test_mode:
        if has_webhook_notification:
            send_teams_webhook_notification(results, test_mode=True)
        if has_email_notification:
            send_email_notification(results, test_mode=True)

    if failed:
        if not test_mode:
            if has_webhook_notification:
                send_teams_webhook_notification(results)
            if has_email_notification:
                send_email_notification(results)
        print(f"⚠  {len(failed)} service(s) reported errors.")
        return 1

    if test_mode:
        print("✅ All services are healthy. Test notification sent.")
        return 0

    print("✅ All services are healthy.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
