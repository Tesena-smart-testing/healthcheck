"""
Weekly security check script.

Reads services from services.csv and generates a standalone security report:
  - docs/security.html
  - docs/security-data/security-<timestamp>.json
"""

import csv
import html as html_lib
import json
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

import requests
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).parent
SERVICES_CSV = REPO_ROOT / "services.csv"
SECURITY_DATA_DIR = REPO_ROOT / "docs" / "security-data"
SECURITY_HTML = REPO_ROOT / "docs" / "security.html"
RETENTION_DAYS = 35
LOGO_URL = "assets/tesena-logo.png"
PRAGUE_TZ = ZoneInfo("Europe/Prague")

SEVERITY_ORDER = {"critical": 4, "high": 3, "medium": 2, "low": 1, "unknown": 0}
STATUS_CLASS = {"ok": "ok", "warning": "warning", "error": "error", "unknown": "unknown"}
STATUS_ICON = {"ok": "✅", "warning": "🟡", "error": "❌", "unknown": "⚪"}
STATUS_PRIORITY = {"unknown": 0, "ok": 1, "warning": 2, "error": 3}

SESSION = requests.Session()
SESSION.headers.update(
    {
        "User-Agent": "tesena-healthcheck-security/1.0",
        "Accept": "application/json",
    }
)
github_token = os.environ.get("SECURITY_GITHUB_TOKEN", "").strip() or os.environ.get("GITHUB_TOKEN", "").strip()
if github_token:
    SESSION.headers["Authorization"] = f"Bearer {github_token}"

_PHP_CVE_FEED_CACHE: dict[str, dict[str, Any] | list[Any] | None] = {}


def _fmt_display(dt: datetime) -> str:
    return dt.astimezone(PRAGUE_TZ).strftime("%Y-%m-%d %H:%M %Z")


def _today() -> date:
    return datetime.now(timezone.utc).date()


def _is_none_value(value: str) -> bool:
    return value.strip().upper() in {"", "NONE", "N/A", "-"}


def _parse_repo_url(repo_url: str) -> tuple[str, str] | None:
    parsed = urlparse(repo_url.strip())
    if parsed.netloc.lower() != "github.com":
        return None
    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) < 2:
        return None
    return parts[0], parts[1]


def _fetch_json(url: str, timeout: float = 20.0) -> dict[str, Any] | list[Any] | None:
    try:
        response = SESSION.get(url, timeout=timeout)
        if response.status_code != 200:
            return None
        return response.json()
    except (requests.RequestException, ValueError):
        return None


def _fetch_raw_github_file(owner: str, repo: str, branches: list[str], file_path: str) -> str | None:
    for branch in branches:
        raw_url = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{file_path}"
        try:
            response = SESSION.get(raw_url, timeout=20)
            if response.status_code == 200:
                return response.text
        except requests.RequestException:
            continue
    return None


def _severity_from_cvss(score: str | float | int | None) -> str:
    if score is None:
        return "unknown"
    try:
        value = float(score)
    except (TypeError, ValueError):
        return "unknown"
    if value >= 9.0:
        return "critical"
    if value >= 7.0:
        return "high"
    if value >= 4.0:
        return "medium"
    if value > 0:
        return "low"
    return "unknown"


def _max_status(current: str, candidate: str) -> str:
    if STATUS_PRIORITY.get(candidate, 0) > STATUS_PRIORITY.get(current, 0):
        return candidate
    return current


def _parse_version_tuple(version: str) -> tuple[int, int, int] | None:
    match = re.match(r"^\s*(\d+)\.(\d+)\.(\d+)", version.strip())
    if not match:
        return None
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def _compare_versions(left: str, right: str) -> int | None:
    left_tuple = _parse_version_tuple(left)
    right_tuple = _parse_version_tuple(right)
    if left_tuple is None or right_tuple is None:
        return None
    if left_tuple < right_tuple:
        return -1
    if left_tuple > right_tuple:
        return 1
    return 0


def _matches_version_prefix(version: str, prefix_version: str) -> bool:
    normalized_prefix = prefix_version.replace("*", "").strip(".")
    prefix_parts = [p for p in normalized_prefix.split(".") if p]
    current_tuple = _parse_version_tuple(version)
    if current_tuple is None:
        return False
    compare_parts = [str(current_tuple[0]), str(current_tuple[1]), str(current_tuple[2])]
    return compare_parts[: len(prefix_parts)] == prefix_parts


def _evaluate_cve_version_rule(current_version: str, version_rule: dict[str, Any]) -> bool:
    status = str(version_rule.get("status", "")).strip().lower()
    if status not in {"affected", "unaffected"}:
        return False

    base_version = str(version_rule.get("version", "")).strip()
    less_than = str(version_rule.get("lessThan", "")).strip()
    less_or_equal = str(version_rule.get("lessThanOrEqual", "")).strip()
    greater_than = str(version_rule.get("greaterThan", "")).strip()
    greater_or_equal = str(version_rule.get("greaterThanOrEqual", "")).strip()

    candidate = True
    if base_version:
        if "*" in base_version:
            candidate = _matches_version_prefix(current_version, base_version)
        else:
            cmp_base = _compare_versions(current_version, base_version)
            candidate = cmp_base == 0 if cmp_base is not None else False

    if candidate and greater_than:
        cmp_gt = _compare_versions(current_version, greater_than)
        candidate = cmp_gt is not None and cmp_gt == 1

    if candidate and greater_or_equal:
        cmp_ge = _compare_versions(current_version, greater_or_equal)
        candidate = cmp_ge is not None and cmp_ge >= 0

    if candidate and less_than:
        cmp_lt = _compare_versions(current_version, less_than)
        candidate = cmp_lt is not None and cmp_lt == -1

    if candidate and less_or_equal:
        cmp_le = _compare_versions(current_version, less_or_equal)
        candidate = cmp_le is not None and cmp_le <= 0

    if not candidate:
        return False
    return status == "affected"


def _load_php_cve_feed() -> dict[str, Any] | list[Any] | None:
    cache_key = "php-search"
    if cache_key in _PHP_CVE_FEED_CACHE:
        return _PHP_CVE_FEED_CACHE[cache_key]
    data = _fetch_json("https://cve.circl.lu/api/search/php/php", timeout=40)
    _PHP_CVE_FEED_CACHE[cache_key] = data
    return data


def _extract_php_cve_hits(current_version: str, cycle: str) -> list[dict[str, Any]]:
    data = _load_php_cve_feed()
    if not isinstance(data, dict):
        return []

    result_items: list[list[Any] | tuple[Any, ...]] = []
    source_results = data.get("results")
    if isinstance(source_results, dict):
        for source_rows in source_results.values():
            if isinstance(source_rows, list):
                for row in source_rows:
                    if isinstance(row, (list, tuple)):
                        result_items.append(row)

    hits: list[dict[str, Any]] = []
    for row in result_items:
        if len(row) < 2:
            continue
        cve_id = str(row[0]).upper().strip()
        payload = row[1]
        if not isinstance(payload, dict):
            continue

        cna = payload.get("containers", {}).get("cna", {})
        if not isinstance(cna, dict):
            continue

        affected_entries = cna.get("affected") or []
        if not isinstance(affected_entries, list):
            continue

        is_affected = False
        for affected in affected_entries:
            if not isinstance(affected, dict):
                continue
            product = str(affected.get("product", "")).lower()
            package_name = str(affected.get("packageName", "")).lower()
            if "php" not in product and "php" not in package_name:
                continue

            versions = affected.get("versions") or []
            if not isinstance(versions, list):
                continue
            for version_rule in versions:
                if not isinstance(version_rule, dict):
                    continue
                if _evaluate_cve_version_rule(current_version, version_rule):
                    is_affected = True
                    break
            if is_affected:
                break

        if not is_affected:
            continue

        summary = str(cna.get("title") or "").strip()
        if not summary:
            descriptions = cna.get("descriptions") or []
            if isinstance(descriptions, list):
                for desc in descriptions:
                    if isinstance(desc, dict):
                        value = str(desc.get("value") or "").strip()
                        if value:
                            summary = value
                            break

        references = cna.get("references") or []
        ref_url = ""
        if isinstance(references, list):
            for ref in references:
                if isinstance(ref, dict):
                    candidate = str(ref.get("url") or "").strip()
                    if candidate:
                        ref_url = candidate
                        break

        hits.append(
            {
                "id": cve_id,
                "summary": summary or "No description provided.",
                "reference": ref_url,
                "cycle": cycle,
            }
        )
    return hits
    try:
        value = float(score)
    except (TypeError, ValueError):
        return "unknown"
    if value >= 9.0:
        return "critical"
    if value >= 7.0:
        return "high"
    if value >= 4.0:
        return "medium"
    if value > 0:
        return "low"
    return "unknown"


def _extract_severity(vuln: dict[str, Any]) -> str:
    db_severity = str(vuln.get("database_specific", {}).get("severity", "")).lower().strip()
    if db_severity in SEVERITY_ORDER:
        return db_severity

    for item in vuln.get("severity", []) or []:
        severity = _severity_from_cvss(item.get("score"))
        if severity != "unknown":
            return severity
    return "unknown"


def _extract_npm_packages_from_lockfile(lock_data: dict[str, Any]) -> dict[str, str]:
    packages: dict[str, str] = {}

    if isinstance(lock_data.get("packages"), dict):
        for path, meta in lock_data["packages"].items():
            if path == "" or not isinstance(meta, dict):
                continue
            version = str(meta.get("version", "")).strip()
            if not version:
                continue
            name = str(meta.get("name", "")).strip()
            if not name:
                if "node_modules/" not in path:
                    continue
                name = path.split("node_modules/")[-1]
            packages[name] = version
        return packages

    def _walk(dep_map: dict[str, Any]) -> None:
        for name, metadata in dep_map.items():
            if not isinstance(metadata, dict):
                continue
            version = str(metadata.get("version", "")).strip()
            if version:
                packages[name] = version
            nested = metadata.get("dependencies")
            if isinstance(nested, dict):
                _walk(nested)

    dependencies = lock_data.get("dependencies")
    if isinstance(dependencies, dict):
        _walk(dependencies)
    return packages


def _query_osv_for_npm(packages: dict[str, str]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    package_items = sorted(packages.items())

    for i in range(0, len(package_items), 100):
        chunk = package_items[i : i + 100]
        payload = {
            "queries": [
                {
                    "package": {"ecosystem": "npm", "name": name},
                    "version": version,
                }
                for name, version in chunk
            ]
        }
        try:
            response = SESSION.post("https://api.osv.dev/v1/querybatch", json=payload, timeout=40)
            response.raise_for_status()
            data = response.json()
        except (requests.RequestException, ValueError):
            continue

        for idx, result in enumerate(data.get("results", []) or []):
            if idx >= len(chunk):
                continue
            pkg_name, pkg_version = chunk[idx]
            vulns = result.get("vulns") or []
            for vuln in vulns:
                findings.append(
                    {
                        "package": pkg_name,
                        "version": pkg_version,
                        "vuln_id": vuln.get("id", "N/A"),
                        "summary": (vuln.get("summary") or vuln.get("details") or "").strip(),
                        "severity": _extract_severity(vuln),
                    }
                )
    return findings


def _get_latest_npm_version(package_name: str, cache: dict[str, str | None]) -> str | None:
    if package_name in cache:
        return cache[package_name]

    encoded = quote(package_name, safe="")
    url = f"https://registry.npmjs.org/{encoded}/latest"
    latest = None
    try:
        response = SESSION.get(url, timeout=20)
        if response.status_code == 200:
            latest = str(response.json().get("version") or "").strip() or None
    except (requests.RequestException, ValueError):
        latest = None
    cache[package_name] = latest
    return latest


def _check_php_library(service_name: str, library_value: str) -> dict[str, Any]:
    match = re.search(r"PHP\s+(\d+)\.(\d+)\.(\d+)", library_value, re.IGNORECASE)
    result = {
        "name": service_name,
        "target": library_value,
        "source_type": "library",
        "status": "unknown",
        "summary": "",
        "recommendation": "",
        "details": {},
    }
    if not match:
        result["summary"] = "Unsupported library format. Expected e.g. PHP 8.5.12."
        result["recommendation"] = "Use explicit PHP version format."
        return result

    major, minor, patch = match.groups()
    cycle = f"{major}.{minor}"
    current_version = f"{major}.{minor}.{patch}"
    endpoint = f"https://endoflife.date/api/php/{cycle}.json"
    cycle_data = _fetch_json(endpoint)
    if not isinstance(cycle_data, dict):
        result["summary"] = "Could not fetch PHP lifecycle data."
        result["recommendation"] = "Retry in next scheduled run."
        return result

    latest = str(cycle_data.get("latest", "")).strip()
    support_raw = str(cycle_data.get("support", "")).strip()
    eol_raw = str(cycle_data.get("eol", "")).strip()

    support_date = None
    eol_date = None
    try:
        support_date = date.fromisoformat(support_raw) if support_raw else None
    except ValueError:
        support_date = None
    try:
        eol_date = date.fromisoformat(eol_raw) if eol_raw else None
    except ValueError:
        eol_date = None

    today = _today()
    result["details"] = {
        "latest_in_cycle": latest or None,
        "support_until": support_raw or None,
        "eol": eol_raw or None,
    }

    summary_parts: list[str] = []
    recommendation_parts: list[str] = []
    result["status"] = "ok"

    if eol_date and today > eol_date:
        result["status"] = _max_status(result["status"], "error")
        summary_parts.append(f"PHP {cycle} is end-of-life.")
        recommendation_parts.append("Upgrade to a currently supported PHP branch immediately.")
    elif support_date and today > support_date:
        result["status"] = _max_status(result["status"], "warning")
        summary_parts.append(f"PHP {cycle} no longer receives active security support.")
        recommendation_parts.append("Upgrade to a currently supported PHP branch.")

    if latest and current_version != latest:
        result["status"] = _max_status(result["status"], "warning")
        summary_parts.append(f"Newer patch available in cycle ({latest}).")
        recommendation_parts.append(f"Upgrade from {current_version} to {latest}.")

    cve_hits = _extract_php_cve_hits(current_version, cycle)
    result["details"]["cve_hits"] = cve_hits[:10]
    result["details"]["cve_count"] = len(cve_hits)
    if cve_hits:
        result["status"] = _max_status(result["status"], "error")
        top_ids = ", ".join(hit["id"] for hit in cve_hits[:3])
        summary_parts.append(
            f"Known CVEs affecting this version were found ({len(cve_hits)} total; e.g. {top_ids})."
        )
        recommendation_parts.append("Upgrade to a fixed PHP patch version and review referenced advisories.")
    else:
        summary_parts.append("No known CVEs found for this exact PHP version in the configured CVE feed.")

    if not summary_parts:
        summary_parts.append("No known security support issues detected for this PHP version.")
    if not recommendation_parts:
        recommendation_parts.append("No immediate action required.")

    result["summary"] = " ".join(summary_parts)
    result["recommendation"] = " ".join(dict.fromkeys(recommendation_parts))
    return result


def _choose_repo_branches(owner: str, repo: str) -> list[str]:
    api_url = f"https://api.github.com/repos/{owner}/{repo}"
    repo_info = _fetch_json(api_url)
    candidates: list[str] = []
    if isinstance(repo_info, dict):
        default_branch = str(repo_info.get("default_branch", "")).strip()
        if default_branch:
            candidates.append(default_branch)
    for branch in ("main", "master"):
        if branch not in candidates:
            candidates.append(branch)
    return candidates


def _analyze_repo_dependencies(service_name: str, repo_url: str) -> dict[str, Any]:
    base_result = {
        "name": service_name,
        "target": repo_url,
        "source_type": "repository",
        "status": "unknown",
        "summary": "",
        "recommendation": "",
        "details": {},
    }

    parsed = _parse_repo_url(repo_url)
    if not parsed:
        base_result["summary"] = "Unsupported repository URL. Only github.com URLs are supported."
        base_result["recommendation"] = "Use full GitHub repository URL."
        return base_result

    owner, repo = parsed
    branches = _choose_repo_branches(owner, repo)
    lockfile_text = _fetch_raw_github_file(owner, repo, branches, "package-lock.json")
    if not lockfile_text:
        lockfile_text = _fetch_raw_github_file(owner, repo, branches, "npm-shrinkwrap.json")

    if not lockfile_text:
        package_json = _fetch_raw_github_file(owner, repo, branches, "package.json")
        if package_json:
            base_result["status"] = "warning"
            base_result["summary"] = "No lockfile found; exact dependency versions cannot be fully verified."
            base_result["recommendation"] = "Commit package-lock.json (or npm-shrinkwrap.json) for precise security scanning."
            return base_result
        base_result["status"] = "warning"
        base_result["summary"] = "No npm manifest found in repository root (or repository is not accessible)."
        base_result["recommendation"] = "Add package.json (if this is an npm project) or configure a different scanner."
        return base_result

    try:
        lock_data = json.loads(lockfile_text)
    except json.JSONDecodeError:
        base_result["summary"] = "Dependency lockfile is not valid JSON."
        base_result["recommendation"] = "Fix lockfile format and rerun."
        return base_result

    packages = _extract_npm_packages_from_lockfile(lock_data)
    if not packages:
        base_result["status"] = "warning"
        base_result["summary"] = "No package versions discovered in lockfile."
        base_result["recommendation"] = "Verify lockfile content."
        return base_result

    findings = _query_osv_for_npm(packages)
    severity_counts = {"critical": 0, "high": 0, "medium": 0, "low": 0, "unknown": 0}
    for finding in findings:
        severity = finding["severity"]
        if severity not in severity_counts:
            severity = "unknown"
        severity_counts[severity] += 1

    base_result["details"] = {
        "package_count": len(packages),
        "finding_count": len(findings),
        "severity_counts": severity_counts,
    }

    if not findings:
        base_result["status"] = "ok"
        base_result["summary"] = (
            f"No known vulnerabilities found in analyzed npm lockfile ({len(packages)} packages)."
        )
        base_result["recommendation"] = "No immediate action required."
        return base_result

    highest = "low"
    for severity in ("critical", "high", "medium", "low"):
        if severity_counts[severity] > 0:
            highest = severity
            break

    base_result["status"] = "error" if highest in {"critical", "high"} else "warning"
    base_result["summary"] = (
        "Vulnerabilities detected: "
        f"critical={severity_counts['critical']}, high={severity_counts['high']}, "
        f"medium={severity_counts['medium']}, low={severity_counts['low']}, unknown={severity_counts['unknown']}."
    )

    latest_cache: dict[str, str | None] = {}
    findings_sorted = sorted(
        findings,
        key=lambda x: SEVERITY_ORDER.get(x["severity"], 0),
        reverse=True,
    )
    top_findings = findings_sorted[:5]
    recommendations: list[str] = []
    for item in top_findings:
        latest = _get_latest_npm_version(item["package"], latest_cache)
        if latest:
            recommendations.append(
                f"{item['package']} {item['version']} -> {latest} ({item['severity']}, {item['vuln_id']})"
            )
        else:
            recommendations.append(
                f"{item['package']} {item['version']} ({item['severity']}, {item['vuln_id']})"
            )

    if recommendations:
        base_result["recommendation"] = "Prioritize upgrades: " + "; ".join(recommendations)
    else:
        base_result["recommendation"] = "Review vulnerabilities and update affected dependencies."
    return base_result


def save_results(results: list[dict[str, Any]]) -> Path:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    SECURITY_DATA_DIR.mkdir(parents=True, exist_ok=True)
    out_path = SECURITY_DATA_DIR / f"security-{ts}.json"
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(results, fh, indent=2, ensure_ascii=False)
    print(f"[OK] Security results written to {out_path}")
    return out_path


def cleanup_old_results() -> None:
    cutoff = datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)
    removed = 0
    for file_path in sorted(SECURITY_DATA_DIR.glob("security-*.json")):
        try:
            stamp = file_path.stem.split("security-")[1]
            file_dt = datetime.strptime(stamp, "%Y%m%d-%H%M%S").replace(tzinfo=timezone.utc)
            if file_dt < cutoff:
                file_path.unlink()
                removed += 1
        except (IndexError, ValueError):
            continue
    if removed:
        print(f"[OK] Removed {removed} old security result file(s)")


def generate_security_html(results: list[dict[str, Any]]) -> None:
    ok_count = sum(1 for r in results if r["status"] == "ok")
    warning_count = sum(1 for r in results if r["status"] == "warning")
    error_count = sum(1 for r in results if r["status"] == "error")
    unknown_count = sum(1 for r in results if r["status"] == "unknown")
    generated_at = _fmt_display(datetime.now(timezone.utc))

    rows_html = ""
    for item in results:
        status = item["status"]
        icon = STATUS_ICON.get(status, "⚪")
        css_class = STATUS_CLASS.get(status, "unknown")
        service_name = html_lib.escape(str(item.get("name") or "Unknown service"))
        server_value = str(item.get("server") or "").strip()
        if server_value:
            service_name = f"{service_name} <small>(server: {html_lib.escape(server_value)})</small>"

        target_parts = [f"<code>{html_lib.escape(str(item.get('target') or 'N/A'))}</code>"]
        repository_url = str(item.get("repository_url") or "").strip()
        if repository_url and not _is_none_value(repository_url):
            safe_repo_url = html_lib.escape(repository_url, quote=True)
            target_parts.append(
                f'<div><a href="{safe_repo_url}" target="_blank" rel="noopener noreferrer">GitHub repository</a></div>'
            )

        target_html = "".join(target_parts)
        summary_html = html_lib.escape(str(item.get("summary") or ""))
        recommendation_html = html_lib.escape(str(item.get("recommendation") or ""))
        rows_html += (
            f'<tr class="{css_class}">'
            f"<td>{icon} {status.upper()}</td>"
            f"<td><strong>{service_name}</strong></td>"
            f"<td>{target_html}</td>"
            f"<td>{summary_html}</td>"
            f"<td>{recommendation_html}</td>"
            "</tr>\n"
        )

    if not rows_html:
        rows_html = (
            '<tr class="unknown"><td>⚪ UNKNOWN</td><td colspan="4">'
            "No security entries found in services.csv."
            "</td></tr>"
        )

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<meta http-equiv="refresh" content="3600"/>
<title>Application Security Snapshot</title>
<style>
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    background: #f4f6f9;
    color: #222;
    margin: 0;
    padding: 20px;
  }}
  .container {{
    max-width: 1200px;
    margin: 0 auto;
    background: #fff;
    border-radius: 8px;
    box-shadow: 0 2px 6px rgba(0,0,0,.1);
    padding: 24px;
  }}
  h1 {{
    margin: 0 0 12px;
    color: #00558B;
  }}
  .meta {{
    color: #666;
    margin-bottom: 16px;
    font-size: 0.95em;
  }}
  .stats {{
    display: grid;
    grid-template-columns: repeat(4, minmax(0, 1fr));
    gap: 10px;
    margin-bottom: 18px;
  }}
  .stat {{
    background: #f8fafc;
    border: 1px solid #e8edf5;
    border-radius: 6px;
    padding: 10px 12px;
  }}
  .stat b {{
    display: block;
    font-size: 1.2em;
  }}
  table {{
    width: 100%;
    border-collapse: collapse;
    font-size: 0.92em;
  }}
  thead {{
    background: #e8eaf6;
  }}
  th, td {{
    text-align: left;
    padding: 10px;
    border-bottom: 1px solid #eee;
    vertical-align: top;
  }}
  tr.ok td {{
    background: #f6fff8;
  }}
  tr.warning td {{
    background: #fffbe6;
  }}
  tr.error td {{
    background: #fff5f5;
  }}
  tr.unknown td {{
    background: #f5f7fa;
  }}
  code {{
    background: #f3f4f6;
    border-radius: 4px;
    padding: 2px 5px;
  }}
  .logo-footer {{
    display: flex;
    justify-content: center;
    margin-top: 24px;
  }}
  .title-logo {{
    width: 180px;
    height: 180px;
    object-fit: contain;
  }}
  a {{
    color: #1a73e8;
  }}
  @media (max-width: 900px) {{
    .stats {{
      grid-template-columns: repeat(2, minmax(0, 1fr));
    }}
  }}
</style>
</head>
<body>
  <div class="container">
    <h1>Tesena Security Snapshot</h1>
    <p class="meta">
      Generated: {generated_at} &nbsp;|&nbsp;
      Schedule: weekly (Sunday) &nbsp;|&nbsp;
      Data retention: {RETENTION_DAYS} days &nbsp;|&nbsp;
      <a href="index.html">Open healthcheck dashboard</a>
    </p>

    <div class="stats">
      <div class="stat"><b>{ok_count}</b>OK</div>
      <div class="stat"><b>{warning_count}</b>Warnings</div>
      <div class="stat"><b>{error_count}</b>Issues</div>
      <div class="stat"><b>{unknown_count}</b>Unknown</div>
    </div>

    <table>
      <thead>
        <tr>
          <th>Status</th>
          <th>Service</th>
          <th>Library / Repository</th>
          <th>Finding</th>
          <th>Recommendation</th>
        </tr>
      </thead>
      <tbody>
        {rows_html}
      </tbody>
    </table>

    <div class="logo-footer">
      <img src="{LOGO_URL}" alt="Tesena logo" class="title-logo"/>
    </div>
  </div>
</body>
</html>"""

    SECURITY_HTML.parent.mkdir(parents=True, exist_ok=True)
    with open(SECURITY_HTML, "w", encoding="utf-8") as fh:
        fh.write(html)
    print(f"[OK] Security report written to {SECURITY_HTML}")


def main() -> int:
    if not SERVICES_CSV.exists():
        print(f"✗  services.csv not found at {SERVICES_CSV}", file=sys.stderr)
        return 1

    results: list[dict[str, Any]] = []
    repo_cache: dict[str, dict[str, Any]] = {}

    with open(SERVICES_CSV, newline="", encoding="utf-8") as csvfile:
        reader = csv.DictReader(csvfile, delimiter=";")
        for row in reader:
            name = (row.get("name") or "").strip() or (row.get("url") or "").strip() or "Unknown service"
            library_value = (row.get("library") or "").strip()
            repo_value = (row.get("repozitory") or "").strip()
            server_value = (row.get("server") or "").strip()
            repository_url = None if _is_none_value(repo_value) else repo_value

            if not _is_none_value(library_value):
                print(f"Security checking library for {name} ...", end=" ", flush=True)
                result = _check_php_library(name, library_value)
                result["server"] = server_value
                result["repository_url"] = repository_url
                print(result["status"].upper())
                results.append(result)
                continue

            if not _is_none_value(repo_value):
                print(f"Security checking repository for {name} ...", end=" ", flush=True)
                if repo_value in repo_cache:
                    cached = dict(repo_cache[repo_value])
                    cached["name"] = name
                    result = cached
                else:
                    analyzed = _analyze_repo_dependencies(name, repo_value)
                    repo_cache[repo_value] = dict(analyzed)
                    result = analyzed
                result["server"] = server_value
                result["repository_url"] = repository_url
                print(result["status"].upper())
                results.append(result)
                continue

            results.append(
                {
                    "name": name,
                    "target": "N/A",
                    "source_type": "none",
                    "status": "unknown",
                    "summary": "Neither library nor repository configured.",
                    "recommendation": "Fill library or repozitory in services.csv.",
                    "server": server_value,
                    "repository_url": repository_url,
                    "details": {},
                }
            )

    if not results:
        print("No services found in services.csv", file=sys.stderr)
        return 1

    save_results(results)
    cleanup_old_results()
    generate_security_html(results)

    ok_count = sum(1 for r in results if r["status"] == "ok")
    warning_count = sum(1 for r in results if r["status"] == "warning")
    error_count = sum(1 for r in results if r["status"] == "error")
    print(
        f"[OK] Security check complete: ok={ok_count}, warnings={warning_count}, errors={error_count}, total={len(results)}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
