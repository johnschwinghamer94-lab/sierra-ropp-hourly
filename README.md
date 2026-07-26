# sierra-ropp-hourly (PRIVATE)

Cloud "always-on" backup for the ROPP dashboard's **Hour by Hour** tab. Runs with no
machine on. Raw ServiceTitan reports stay in this **private** repo and never touch the
public dashboard — only the aggregate `hourly.json` is published there.

## Flow
1. Power Automate pushes today's `Revenue by Job Type` + `ROPP TGLs Created` exports
   into `hourly_reports/` (filenames just need to contain `revenue` and `tgls created`).
2. That push triggers `.github/workflows/hourly.yml`.
3. `cloud_hourly.py` counts today's distinct ROPPs/TGLs and PUTs the shared
   `hourly_state.json` + `hourly.json` into the public `sierra-ropp-dashboard` repo.

## One-time setup
1. **Secret:** repo **Settings → Secrets and variables → Actions → New repository secret**
   - Name: `DASHBOARD_TOKEN`
   - Value: a GitHub token with **write access to `sierra-ropp-dashboard`** (classic `repo`
     scope, or a fine-grained token scoped to that repo with Contents: read/write).
2. **Power Automate:** add a step so the hourly report emails also land here (see the
   instructions your assistant provided).

Shares `hourly_state.json` with the PC/Mac capture, so all three fill the same hour-by-hour
series without clobbering each other.
