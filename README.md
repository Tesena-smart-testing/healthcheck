# Healthcheck

Automated monitoring of web applications. Runs hourly via GitHub Actions, checks HTTP status, response time and presence of a specific HTML element via XPath. Results are published to GitHub Pages.

---

## How it works

1. **`services.csv`** defines the list of services to check.
2. **`healthcheck.py`** performs the checks, saves JSON results to `docs/data/` and regenerates the HTML dashboard.
3. **GitHub Actions** runs the check every hour, commits results to the `gh-pages` branch and deploys them to GitHub Pages.
4. On failure, an **e-mail alert** is sent via SendGrid.

---

## GitHub Pages

The dashboard is available at the URL shown in **Settings → Pages** of this repository.

- **`/`** – full historical dashboard with per-service cards and history tables
- **`/summary.html`** – concise summary: list of services and their current status (✅ / ❌)

Pages source is set to **GitHub Actions** (Settings → Pages → Source: GitHub Actions).

---

## services.csv

Semicolon-separated file defining which services to check.

```csv
url;expected_status;max_response_seconds;xpath
https://example.com/;200;4;//title[contains(text(),"Example")]
```

| Column | Description |
|---|---|
| `url` | Full URL including scheme (`https://`) |
| `expected_status` | Expected HTTP status code (usually `200`) |
| `max_response_seconds` | Maximum allowed response time in seconds |
| `xpath` | XPath expression that must match an element in the **server-rendered** HTML (leave empty to skip). **Note:** XPath does not work for JS-rendered content (SPA). Use `//title[...]` or `//meta[@name="description"]` for SPA apps. |

---

## GitHub Secrets

Configure the following secrets in **Settings → Secrets and variables → Actions**:

### E-mail notifications (SMTP)

| Secret | Description |
|---|---|
| `SMTP_HOST` | SMTP server hostname (e.g. `smtp.gmail.com`, `smtp.office365.com` for MS 365) |
| `SMTP_PORT` | SMTP port, usually `587` (TLS) or `465` (SSL) |
| `SMTP_USER` | SMTP username / login e-mail |
| `SMTP_PASSWORD` | SMTP password or app password (for Microsoft accounts, use an [app password](https://support.microsoft.com/en-us/account-billing/using-app-passwords-with-apps-that-don-t-support-two-step-verification-5896ed9b8924) instead of your account password to avoid 3-month rotation issues) |
| `TEAMS_EMAIL` | Recipient e-mail address (e.g. MS Teams channel e-mail or any mailbox) |

---

## GitHub Actions workflow

File: `.github/workflows/healthcheck.yml`

- Runs **every hour** (cron `0 * * * *`) and on manual trigger (`workflow_dispatch`).
- Results are committed to the `gh-pages` branch (only `docs/` folder).
- Data older than **14 days** is automatically deleted (`RETENTION_DAYS = 14` in `healthcheck.py`).
- The `deploy` job has an automatic **retry** if GitHub Pages deployment fails transiently.

### Workflow env variable

| Variable | Default | Description |
|---|---|---|
| `PAGES_BRANCH` | `gh-pages` | Branch where generated `docs/` is committed |

---

## Local development

```bash
pip install -r requirements.txt
python healthcheck.py
```

Results are written to `docs/data/results-<timestamp>.json` and `docs/index.html` / `docs/summary.html`.

To test e-mail sending locally, set the environment variables:

```bash
export SENDGRID_API_KEY=SG.xxx
export SENDGRID_FROM_EMAIL=alerts@yourdomain.com
export TEAMS_EMAIL=recipient@yourdomain.com
python healthcheck.py
```

---

## XPath note for SPA applications

Many modern apps (React, Next.js, Angular…) render their main content via JavaScript. The healthcheck uses `requests + lxml` without a browser, so **only server-rendered HTML** is available for XPath evaluation.

✅ Works: `//title[...]`, `//meta[@name="description"]/@content`
❌ Does not work: `//h1[...]`, `//div[@class="hero"]` (rendered by JS)
