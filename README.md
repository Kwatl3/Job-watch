# Job Watch

A GitHub-hosted job dashboard. A GitHub Actions workflow checks the companies listed in
`companies.yml`, stores the result in `docs/jobs.json`, and GitHub Pages serves `docs/index.html`
as the dashboard.

## Folder layout (must be exactly this)

```
companies.yml
requirements.txt
README.md
.github/workflows/fetch-jobs.yml
scripts/fetch_jobs.py
docs/index.html
```

If the repo shows `fetch-jobs.yml`, `fetch_jobs.py` or `index.html` at the top level, the upload lost
its folders. On GitHub, open the file, click the pencil icon, and type the folder in front of the file
name (for example `docs/` in front of `index.html`) to move it.

## Setup

1. Create a public GitHub repository and upload everything above with its folders.
2. **Settings → Pages**: Source = *Deploy from a branch*, Branch = `main`, Folder = `/docs`.
3. **Actions tab → Fetch jobs → Run workflow** once. The first run reads every company, so it can take
   several minutes.
4. Open `https://<you>.github.io/<repo>/`.

## Where the jobs come from

- **`arbeitsagentur`** (most companies): the official German job-agency feed. It lists jobs located in
  Germany by employer name, so it works for employers whose own career site cannot be read
  automatically. It only contains jobs the employer posts there, and it does not cover jobs outside
  Germany.
- **Company career systems** (Workday, SmartRecruiters, Greenhouse, Lever, Personio, plain HTML): used
  where a company's own feed is known, for example Airbus (Workday) and CERN (SmartRecruiters).

## Tiers

Companies with `tier: slow` are checked every 6 hours, all others every 30 minutes. Move a company
between the two by adding or removing `tier: slow` on its line.

## Your profile and filters

At the top of `companies.yml`, the `profile` section describes the jobs you want:

- `core` words (process, manufacturing and production engineering, product development, industrialisation,
  configuration management, FMEA, Lean) mark a job as a **★ BEST MATCH**.
- `related` words (engineer, quality, technology, development and similar) keep other relevant jobs
  without the star.
- A job is shown only if its title contains a core or related word and none of the `exclude` words
  (internships, software, sales and similar).

Both lists are plain words you can add to or remove. The dashboard has a "Best matches only" switch.

## How it behaves

- **New**: a job shows NEW for 48 hours after it first appears. The first fetch of a company is a
  baseline, so its existing jobs show as "posted" or "tracked since" instead of NEW.
- **Closed**: jobs that disappear stay visible (greyed out, via "Show closed") for 14 days.
- **Failures**: if one company errors, its previous jobs stay and the error shows under "Company
  status". Other companies are unaffected.
- **Commits**: `jobs.json` is only committed when something changed. The header shows the time of the
  last change; the Actions tab shows every run.

## Limits

- GitHub scheduled workflows can start late, and GitHub pauses them after 60 days without repository
  activity. Any push, or re-enabling in the Actions tab, restarts them.
- Respect each source's terms of use. This is meant for personal use.
