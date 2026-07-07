# Healthcheck

Automated monitoring of web applications. Runs hourly via GitHub Actions, checks HTTP status, response time and presence of a specific HTML element via XPath. Results are published to GitHub Pages.

---

## How it works

1. **`services.csv`** defines the list of services to check.
2. **`healthcheck.py`** performs the checks, saves JSON results to `docs/data/` and regenerates the HTML dashboard.
3. **GitHub Actions** runs the check every hour, commits results to the `gh-pages` branch and deploys them to GitHub Pages.
4. On failure, a **notification** is sent to MS Teams (Incoming Webhook recommended, SMTP optional fallback).
5. A manual **test mode** can send a test message even when all services are healthy.

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

### MS Teams notifications (recommended)

| Secret | Description |
|---|---|
| `TEAMS_WEBHOOK_URL` | Incoming Webhook URL for your MS Teams channel |

When `TEAMS_WEBHOOK_URL` is set, notifications are posted directly into the Teams channel and do not depend on SMTP e-mail restrictions.

### E-mail notifications (SMTP)

| Secret | Description |
|---|---|
| `SMTP_HOST` | SMTP server hostname (e.g. `smtp.gmail.com`, `smtp.office365.com` for MS 365) |
| `SMTP_PORT` | SMTP port, usually `587` (TLS) or `465` (SSL) |
| `SMTP_USER` | SMTP username / login e-mail |
| `SMTP_PASSWORD` | SMTP password or app password (for Microsoft accounts, use an [app password](https://support.microsoft.com/en-us/account-billing/using-app-passwords-with-apps-that-don-t-support-two-step-verification-5896ed9b8924) instead of your account password to avoid 3-month rotation issues) |
| `TEAMS_EMAIL` | Recipient e-mail address (e.g. MS Teams channel e-mail or any mailbox; optional if webhook is used) |

SMTP is optional and can be used as a fallback channel if needed.

### How to create a Teams Incoming Webhook

Microsoft is migrating from legacy Office 365 Connectors to Power Automate Workflows. Use this method:

1. Open your **Teams** → required **channel**.
2. Click **⋯** (More options) next to the channel name.
3. Select **Workflows**.
4. Search for the template **"Send webhook alerts to a channel"** or create a new workflow:
   - Trigger: **"When a Teams webhook request is received"**
   - Action: **"Post message in a chat or channel"** → select the channel
5. Save the workflow.
6. After saving, you'll see **Copy webhook URL** → click to copy.
7. Save the webhook URL in GitHub Actions secret as `TEAMS_WEBHOOK_URL`.

**Note:** If your tenant still has legacy Connectors available, you can also use:
- Channel options → **Connectors** → **Incoming Webhook** (older method, being phased out)

### Microsoft 365 / Outlook app password setup

If you want to send alerts as your own mailbox, for example `tomas.hak@tesena.com`, use SMTP with Microsoft 365.

Recommended values:

| Secret | Value |
|---|---|
| `SMTP_HOST` | `smtp.office365.com` |
| `SMTP_PORT` | `587` |
| `SMTP_USER` | your full mailbox address, e.g. `tomas.hak@tesena.com` |
| `SMTP_PASSWORD` | app password generated for your Microsoft account |

Steps to create the app password:

1. Sign in to your Microsoft security page: <https://mysignins.microsoft.com/security-info> or <https://account.activedirectory.windowsazure.com/Proofup.aspx>.
2. Confirm that **MFA / two-step verification** is already enabled for your account.
3. Open **Security info**.
4. Choose **Add sign-in method**.
5. Select **App password**.
6. Create the password and copy the generated value immediately.
7. Save it into GitHub Actions secret `SMTP_PASSWORD`.
8. Set `SMTP_USER` to your full mailbox address, for example `tomas.hak@tesena.com`.

Notes:

- Microsoft shows the app password only once, so store it immediately.
- If **App password** is not offered, your tenant may have disabled it. In that case ask your Microsoft 365 admin to allow SMTP AUTH for your mailbox, or use another approved mail relay.
- Some tenants also block SMTP AUTH globally or per mailbox. If login fails even with a valid app password, ask the admin to verify SMTP AUTH is enabled for `tomas.hak@tesena.com`.

---

## GitHub Actions workflow

File: `.github/workflows/healthcheck.yml`

- Runs **every hour** (cron `0 * * * *`) and on manual trigger (`workflow_dispatch`).
- Manual trigger supports a **`test_mode`** input. When enabled, the workflow sends a test notification even if all services are healthy.
- Results are committed to the `gh-pages` branch (only `docs/` folder).
- Data older than **14 days** is automatically deleted (`RETENTION_DAYS = 14` in `healthcheck.py`).
- The `deploy` job has an automatic **retry** if GitHub Pages deployment fails transiently.

### Test mode

Use test mode when you want to verify that notification delivery works.

GitHub Actions:

1. Open the **Healthcheck** workflow in GitHub Actions.
2. Click **Run workflow**.
3. Enable **`test_mode`**.
4. Start the workflow.
5. A message with subject starting with **`[Healthcheck TEST]`** should arrive in the configured destination.

Behavior:

- If all services are healthy, the workflow stays successful and still sends the test message.
- If some service is failing, the workflow still reports the failure, but the notification is marked as a **test** run.
- Test mode is meant for manual verification and does not affect the hourly scheduled runs.
- If `TEAMS_WEBHOOK_URL` is configured, test message is posted to Teams channel.
- If SMTP secrets are configured, test e-mail is also sent.

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
export TEAMS_WEBHOOK_URL=https://outlook.office.com/webhook/...
export SMTP_HOST=smtp.office365.com
export SMTP_PORT=587
export SMTP_USER=tomas.hak@tesena.com
export SMTP_PASSWORD=your_app_password
export TEAMS_EMAIL=recipient@yourdomain.com
export HEALTHCHECK_TEST_MODE=1
python healthcheck.py
```

If `HEALTHCHECK_TEST_MODE=1` is set, the script sends a test notification even when all services are healthy.
If `TEAMS_WEBHOOK_URL` is set, Teams notification is sent directly via webhook.

---

## Troubleshooting

### SMTP Authentication error: 5.7.139

**Error message:** `(535, b'5.7.139 Authentication unsuccessful, the request did not meet the criteria to be authenticated successfully...')`

**Cause:** The Microsoft 365 tenant has SMTP AUTH disabled globally or for your mailbox.

**Recommendation:** Prefer `TEAMS_WEBHOOK_URL` for Teams channel notifications; it avoids SMTP AUTH restrictions.

**Solutions:**

1. **Ask your IT admin** to enable SMTP AUTH for your mailbox (`tomas.hak@tesena.com`) or globally in the tenant:
   - Microsoft 365 Admin Center → Settings → Mail flow → authenticated SMTP
   - Or: Exchange Online → Authentication policies → Allow Basic Auth SMTP

2. **Use a generic account instead** (recommended for production):
   - Ask IT to create a service account, e.g. `alerts@tesena.com`
   - Ensure SMTP AUTH is enabled for this account
   - Use that account for GitHub Secrets (change `SMTP_USER` and `SMTP_PASSWORD`)

3. **Alternative SMTP servers** you can try:
   - `smtp.office365.com` port 587 (legacy, may be blocked)
   - `smtp-mail.outlook.com` port 587 (newer, similar restrictions)
   - If neither works, your tenant likely enforces the policy globally

### No test message arrives after enabling test_mode

- Verify `TEAMS_EMAIL` is correct and the mailbox/Teams channel accepts e-mail.
- Check the workflow run output for **"Failed to send notification e-mail"** errors.
- If SMTP connection fails, check `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD` secrets are set.
- Confirm your firewall/network allows outbound SMTP port 587 (TLS).

### Service shows HTTP 500 but is actually working

- This can happen if the host's disk is full or under high load.
- Run the healthcheck manually with `workflow_dispatch` to verify current state.
- Check the service's logs for details.

---

## XPath note for SPA applications

Many modern apps (React, Next.js, Angular…) render their main content via JavaScript. The healthcheck uses `requests + lxml` without a browser, so **only server-rendered HTML** is available for XPath evaluation.

✅ Works: `//title[...]`, `//meta[@name="description"]/@content`
❌ Does not work: `//h1[...]`, `//div[@class="hero"]` (rendered by JS)
