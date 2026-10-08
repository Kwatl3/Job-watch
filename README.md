# Job Watch

A GitHub-hosted job dashboard. A GitHub Actions workflow checks the career sites listed in
`companies.yml` every 30 minutes, stores the result in `docs/jobs.json`, and GitHub Pages serves
`docs/index.html` as the dashboard.

## Setup (about 5 minutes)

1. Create a new GitHub repository (private works for the Actions part; GitHub Pages on a private
   repo needs a paid plan, so use a public repo if you are on the free plan).
2. Push this folder to it:
   ```bash
   cd job-dashboard
   git init -b main
   git add .
   git commit -m "Initial job dashboard"
   git remote add origin https://github.com/<you>/<repo>.git
   git push -u origin main
   ```
3. **Settings → Pages**: Source = *Deploy from a branch*, Branch = `main`, Folder = `/docs`.
4. **Actions tab → Fetch jobs → Run workflow** once to create the first `jobs.json`.
5. Open `https://<you>.github.io/<repo>/`.

## Adding companies

Edit `companies.yml`, commit, and the workflow runs immediately. Supported types:

| type | where to find the values |
|---|---|
| `workday` | career URL `https://<host>/en-US/<site>`; tenant is the first part of the host |
| `greenhouse` | `boards.greenhouse.io/<board>` |
| `lever` | `jobs.lever.co/<site>` |
| `smartrecruiters` | `jobs.smartrecruiters.com/<company_id>` |
| `personio` | `<subdomain>.jobs.personio.de` |
| `html` | any static career page, using CSS selectors |

Pages that load their jobs with JavaScript only (many custom career sites) cannot be read by the
`html` type. Open the page's network tab to see whether it calls a JSON API; if it is one of the
systems above, use that type instead.

Global or per-company `include`, `exclude` and `locations` keywords filter by job title and location.

## How it behaves

- **New**: a job shows a NEW badge for 48 hours after it first appears. The first fetch for a
  company is a baseline, so its existing jobs are shown as "tracked since" instead of NEW.
- **Closed**: jobs that disappear stay visible (greyed out, via "Show closed") for 14 days.
- **Failures**: if one company errors, its previous jobs stay and the failure shows under
  "Company status". Other companies are unaffected.
- **Commits**: `jobs.json` is only committed when something changed, so you do not get 48 commits a day.
  The dashboard header therefore shows the time of the last change, not the last check; the
  Actions tab shows every run.

## Limits to know about

- GitHub scheduled workflows can start several minutes late, and GitHub disables them after
  60 days without repository activity. Any push, or re-enabling in the Actions tab, restarts them.
- Respect each site's terms of use. This checks a handful of pages every 30 minutes for personal use.

## Local test

```bash
pip install -r requirements.txt
python scripts/fetch_jobs.py
python -m http.server -d docs 8000   # then open http://localhost:8000
```
