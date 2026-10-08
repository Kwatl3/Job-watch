#!/usr/bin/env python3
"""Fetch jobs for every company in companies.yml and merge them into docs/jobs.json.

State rules:
- A job is identified by (company, id). First sighting sets first_seen.
- The first successful fetch of a company is a baseline: its jobs are flagged
  "seed" so the dashboard does not shout "NEW" for everything on day one.
- A job missing from a successful fetch is marked closed and dropped after
  closed_retention_days.
- If a company fetch fails, its previous jobs are left untouched.
- The file is only rewritten when something real changed, so the repo history
  is not filled with one commit per run.
"""
from __future__ import annotations

import json
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin

import requests
import yaml
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "companies.yml"
OUT_PATH = ROOT / "docs" / "jobs.json"
UA = "Mozilla/5.0 (compatible; personal-job-dashboard/1.0)"
TIMEOUT = 30

session = requests.Session()
session.headers.update({"User-Agent": UA, "Accept": "application/json, text/html, */*"})


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


# ---------------------------------------------------------------- adapters ---
# Every adapter returns a list of dicts: id, title, location, url, posted (optional).

def fetch_workday(c: dict) -> list[dict]:
    host, tenant, site = c["host"], c["tenant"], c["site"]
    api = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"
    jobs, offset, limit = [], 0, 20
    for _ in range(int(c.get("max_pages", 15))):
        body = {"appliedFacets": {}, "limit": limit, "offset": offset,
                "searchText": c.get("search", "")}
        r = session.post(api, json=body, timeout=TIMEOUT)
        r.raise_for_status()
        data = r.json()
        postings = data.get("jobPostings", [])
        for p in postings:
            path = p.get("externalPath", "")
            jobs.append({
                "id": path or p.get("title", ""),
                "title": p.get("title", "").strip(),
                "location": p.get("locationsText", "") or "",
                "url": f"https://{host}/en-US/{site}{path}",
                "posted": p.get("postedOn", "") or "",
            })
        offset += limit
        if not postings or offset >= data.get("total", 0):
            break
    return jobs


def fetch_greenhouse(c: dict) -> list[dict]:
    r = session.get(f"https://boards-api.greenhouse.io/v1/boards/{c['board']}/jobs", timeout=TIMEOUT)
    r.raise_for_status()
    return [{
        "id": str(j["id"]),
        "title": j.get("title", "").strip(),
        "location": (j.get("location") or {}).get("name", ""),
        "url": j.get("absolute_url", ""),
        "posted": j.get("updated_at", ""),
    } for j in r.json().get("jobs", [])]


def fetch_lever(c: dict) -> list[dict]:
    r = session.get(f"https://api.lever.co/v0/postings/{c['site']}?mode=json", timeout=TIMEOUT)
    r.raise_for_status()
    out = []
    for j in r.json():
        created = j.get("createdAt")
        posted = datetime.fromtimestamp(created / 1000, timezone.utc).date().isoformat() if created else ""
        out.append({
            "id": j["id"],
            "title": j.get("text", "").strip(),
            "location": (j.get("categories") or {}).get("location", "") or "",
            "url": j.get("hostedUrl", ""),
            "posted": posted,
        })
    return out


def fetch_smartrecruiters(c: dict) -> list[dict]:
    cid, out, offset = c["company_id"], [], 0
    while True:
        r = session.get(f"https://api.smartrecruiters.com/v1/companies/{cid}/postings",
                        params={"limit": 100, "offset": offset}, timeout=TIMEOUT)
        r.raise_for_status()
        data = r.json()
        for p in data.get("content", []):
            loc = p.get("location") or {}
            out.append({
                "id": p["id"],
                "title": p.get("name", "").strip(),
                "location": ", ".join(x for x in (loc.get("city"), (loc.get("country") or "").upper()) if x),
                "url": f"https://jobs.smartrecruiters.com/{cid}/{p['id']}",
                "posted": (p.get("releasedDate") or "")[:10],
            })
        offset += 100
        if offset >= data.get("totalFound", 0) or not data.get("content"):
            break
    return out


def fetch_personio(c: dict) -> list[dict]:
    sub = c["subdomain"]
    r = session.get(f"https://{sub}.jobs.personio.de/xml", timeout=TIMEOUT)
    r.raise_for_status()
    root = ET.fromstring(r.content)
    out = []
    for pos in root.iter("position"):
        pid = (pos.findtext("id") or "").strip()
        out.append({
            "id": pid,
            "title": (pos.findtext("name") or "").strip(),
            "location": (pos.findtext("office") or "").strip(),
            "url": f"https://{sub}.jobs.personio.de/job/{pid}",
            "posted": (pos.findtext("createdAt") or "")[:10],
        })
    return out


def fetch_html(c: dict) -> list[dict]:
    r = session.get(c["url"], timeout=TIMEOUT)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    out = []
    for item in soup.select(c["item"]):
        t = item.select_one(c["title"])
        a = item.select_one(c["link"]) if c.get("link") else (item if item.name == "a" else item.find("a"))
        if not t or not a or not a.get("href"):
            continue
        loc = item.select_one(c["location"]) if c.get("location") else None
        url = urljoin(c["url"], a["href"])
        out.append({
            "id": url,
            "title": t.get_text(" ", strip=True),
            "location": loc.get_text(" ", strip=True) if loc else "",
            "url": url,
            "posted": "",
        })
    return out


ADAPTERS = {
    "workday": fetch_workday,
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "smartrecruiters": fetch_smartrecruiters,
    "personio": fetch_personio,
    "html": fetch_html,
}


# ----------------------------------------------------------------- filters ---

def passes_filters(job: dict, company: dict, global_filters: dict) -> bool:
    def pick(key):
        return company.get(key, global_filters.get(key) or [])

    title = job["title"].lower()
    loc = job["location"].lower()
    include, exclude, locations = pick("include"), pick("exclude"), pick("locations")
    if include and not any(k.lower() in title for k in include):
        return False
    if exclude and any(k.lower() in title for k in exclude):
        return False
    if locations and loc and not any(k.lower() in loc for k in locations):
        return False
    return True


# ------------------------------------------------------------------- merge ---

def merge(old: dict, fetched: dict[str, list[dict]], errors: dict[str, str],
          cfg: dict, now: str) -> dict:
    """Pure function: old state + fetch results -> new state (no timestamps for 'checked')."""
    retention = timedelta(days=int(cfg.get("closed_retention_days", 14)))
    now_dt = datetime.fromisoformat(now)
    old_jobs = {(j["company"], j["id"]): j for j in old.get("jobs", [])}
    known_companies = {j["company"] for j in old.get("jobs", [])} | set(old.get("baselined", []))
    baselined = set(old.get("baselined", []))
    result: dict[tuple, dict] = {}

    # Keep everything for companies that failed or are no longer configured to be fetched.
    configured = {c["name"] for c in cfg["companies"]}
    for key, job in old_jobs.items():
        if job["company"] in errors or job["company"] not in configured:
            result[key] = job

    for company, jobs in fetched.items():
        is_baseline = company not in known_companies
        seen = set()
        for j in jobs:
            key = (company, j["id"])
            seen.add(key)
            prev = old_jobs.get(key)
            if prev:
                entry = {**prev, **{k: j[k] for k in ("title", "location", "url", "posted")}}
                entry.pop("closed_at", None)  # reopened
            else:
                entry = {"company": company, **j, "first_seen": now}
                if is_baseline:
                    entry["seed"] = True
            result[key] = entry
        baselined.add(company)
        for key, prev in old_jobs.items():
            if key[0] == company and key not in seen:
                entry = dict(prev)
                entry.setdefault("closed_at", now)
                closed = datetime.fromisoformat(entry["closed_at"])
                if now_dt - closed <= retention:
                    result[key] = entry

    jobs_sorted = sorted(result.values(), key=lambda j: (j["first_seen"], j["title"]), reverse=True)
    status = dict(old.get("companies", {}))
    for name, jobs in fetched.items():
        status[name] = {"ok": True, "count": len(jobs)}
    for name, msg in errors.items():
        status[name] = {"ok": False, "error": msg[:200],
                        "count": status.get(name, {}).get("count", 0)}
    status = {k: v for k, v in status.items() if k in configured}
    return {"new_window_hours": int(cfg.get("new_window_hours", 48)),
            "baselined": sorted(baselined & configured),
            "companies": status, "jobs": jobs_sorted}


def main() -> int:
    cfg = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    companies = cfg.get("companies") or []
    if not companies:
        print("No companies configured in companies.yml")
        return 1
    global_filters = cfg.get("filters") or {}

    fetched: dict[str, list[dict]] = {}
    errors: dict[str, str] = {}
    for c in companies:
        name, ctype = c["name"], c.get("type")
        adapter = ADAPTERS.get(ctype)
        if not adapter:
            errors[name] = f"unknown type '{ctype}'"
            print(f"[{name}] unknown type {ctype!r}", file=sys.stderr)
            continue
        try:
            jobs = [j for j in adapter(c) if j["id"] and j["title"] and passes_filters(j, c, global_filters)]
            fetched[name] = jobs
            print(f"[{name}] {len(jobs)} jobs")
        except Exception as exc:  # one broken company must not stop the others
            errors[name] = f"{type(exc).__name__}: {exc}"
            print(f"[{name}] FAILED: {errors[name]}", file=sys.stderr)

    old = json.loads(OUT_PATH.read_text(encoding="utf-8")) if OUT_PATH.exists() else {}
    now = now_iso()
    new_state = merge({k: v for k, v in old.items() if k != "updated"}, fetched, errors, cfg, now)

    if old and {k: v for k, v in old.items() if k != "updated"} == new_state:
        print("No changes.")
    else:
        new_state["updated"] = now
        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        OUT_PATH.write_text(json.dumps(new_state, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"Wrote {len(new_state['jobs'])} jobs.")

    # Fail the run only if every company failed (so a broken setup shows red in Actions).
    return 1 if errors and not fetched else 0


if __name__ == "__main__":
    sys.exit(main())
