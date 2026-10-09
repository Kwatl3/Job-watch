#!/usr/bin/env python3
"""Fetch jobs for the companies in companies.yml and merge them into docs/jobs.json.

Usage: python scripts/fetch_jobs.py [--tier fast|slow|all]

Tiers: every company has a tier (default "fast"). The 30-minute schedule runs the
"fast" tier, a 6-hourly schedule runs the "slow" tier, so hundreds of companies do
not hammer the sources every half hour.

State rules:
- A job is identified by (company, id). First sighting sets first_seen.
- The first successful fetch of a company is a baseline: its jobs are flagged
  "seed" so the dashboard does not shout "NEW" for everything on day one.
- A job missing from a successful fetch is marked closed and dropped after
  closed_retention_days.
- Companies that failed or were not part of this run keep their previous jobs.
- The file is only rewritten when something real changed, so the repo history
  is not filled with one commit per run.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import threading
import time
import unicodedata
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin

import requests
import yaml
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "companies.yml"
OUT_PATH = ROOT / "docs" / "jobs.json"
UA = "Mozilla/5.0 (compatible; personal-job-dashboard/1.0)"
TIMEOUT = 30

_tls = threading.local()


def sess() -> requests.Session:
    """One requests session per thread, with retries on 429/5xx."""
    s = getattr(_tls, "s", None)
    if s is None:
        s = requests.Session()
        s.headers.update({"User-Agent": UA, "Accept": "application/json, text/html, */*"})
        retry = Retry(total=3, backoff_factor=1.0, status_forcelist=(429, 500, 502, 503, 504),
                      allowed_methods=None)
        s.mount("https://", HTTPAdapter(max_retries=retry))
        _tls.s = s
    return s


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


# ----------------------------------------------------------- name matching ---

def words(s: str) -> list[str]:
    """Lower-case alphanumeric words with accents removed: 'Alfred Kärcher SE' -> [alfred, karcher, se]."""
    s = unicodedata.normalize("NFKD", (s or "").lower().replace("ß", "ss"))
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return re.findall(r"[a-z0-9]+", s)


def employer_matches(found: str, wanted: list[str]) -> bool:
    """True if the employer name returned by the source contains one of the wanted names
    as whole words ('Winkel' does not match 'Winkelmann', 'Rolls Royce' matches 'Rolls-Royce Deutschland')."""
    fw = words(found)
    for w in wanted:
        ww = words(w)
        if not ww:
            continue
        for i in range(len(fw) - len(ww) + 1):
            if fw[i:i + len(ww)] == ww:
                return True
    return False


# ---------------------------------------------------------------- adapters ---
# Every adapter returns a list of dicts: id, title, location, url, posted (optional).

BA_URL = "https://rest.arbeitsagentur.de/jobboerse/jobsuche-service/pc/v6/jobs"
BA_HEADERS = {"X-API-Key": "jobboerse-jobsuche"}  # public key documented by the Bundesagentur


def fetch_arbeitsagentur(c: dict) -> list[dict]:
    """Official job-agency feed (arbeitsagentur.de Jobbörse): jobs in Germany by employer name.

    The feed's own employer filter needs the exact registered name, so we search the company
    name as free text and keep only results whose employer name contains it as whole words."""
    employer = c["employer"]
    wanted = [employer] + list(c.get("employer_match", []))
    size = 100
    out: dict[str, dict] = {}
    for page in range(1, int(c.get("max_pages", 3)) + 1):
        params = {"was": (employer + " " + c.get("keywords", "")).strip(), "page": page, "size": size,
                  "veroeffentlichtseit": int(c.get("days", 100)),
                  "angebotsart": 1}  # 1 = regular jobs (no apprenticeships / internships)
        if c.get("where"):
            params["wo"] = c["where"]
            params["umkreis"] = int(c.get("radius_km", 50))
        r = sess().get(BA_URL, params=params, headers=BA_HEADERS, timeout=TIMEOUT)
        if r.status_code == 400 and size == 100:  # some deployments cap page size at 50
            size = 50
            params["size"] = size
            r = sess().get(BA_URL, params=params, headers=BA_HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        data = r.json()
        items = data.get("ergebnisliste") or []
        for it in items:
            if not employer_matches(it.get("firma", ""), wanted):
                continue
            ref = it.get("referenznummer")
            if not ref:
                continue
            adr = ((it.get("stellenlokationen") or [{}])[0].get("adresse")) or {}
            region = (adr.get("region") or "").replace("_", " ").title()
            out[ref] = {
                "id": ref,
                "title": (it.get("stellenangebotsTitel") or it.get("hauptberuf") or "").strip(),
                "location": ", ".join(x for x in (adr.get("ort"), region) if x),
                "url": f"https://www.arbeitsagentur.de/jobsuche/jobdetail/{ref}",
                "posted": it.get("datumErsteVeroeffentlichung")
                          or (it.get("veroeffentlichungszeitraum") or {}).get("von", "") or "",
            }
        if len(items) < size or page * size >= int(data.get("maxErgebnisse", 0) or 0):
            break
        time.sleep(0.2)
    return list(out.values())


def fetch_workday(c: dict) -> list[dict]:
    """Workday career site. `search` may be one text or a list of texts (results are merged)."""
    host, tenant, site = c["host"], c["tenant"], c["site"]
    api = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"
    searches = c.get("search", "")
    searches = searches if isinstance(searches, list) else [searches]
    jobs: dict[str, dict] = {}
    for text in searches:
        offset, limit = 0, 20
        for _ in range(int(c.get("max_pages", 15))):
            body = {"appliedFacets": {}, "limit": limit, "offset": offset, "searchText": text}
            r = sess().post(api, json=body, timeout=TIMEOUT)
            r.raise_for_status()
            data = r.json()
            postings = data.get("jobPostings", [])
            for p in postings:
                path = p.get("externalPath", "")
                jid = path or p.get("title", "")
                jobs[jid] = {
                    "id": jid,
                    "title": p.get("title", "").strip(),
                    "location": p.get("locationsText", "") or "",
                    "url": f"https://{host}/en-US/{site}{path}",
                    "posted": p.get("postedOn", "") or "",
                }
            offset += limit
            if not postings or offset >= data.get("total", 0):
                break
    return list(jobs.values())


def fetch_greenhouse(c: dict) -> list[dict]:
    r = sess().get(f"https://boards-api.greenhouse.io/v1/boards/{c['board']}/jobs", timeout=TIMEOUT)
    r.raise_for_status()
    return [{
        "id": str(j["id"]),
        "title": j.get("title", "").strip(),
        "location": (j.get("location") or {}).get("name", ""),
        "url": j.get("absolute_url", ""),
        "posted": j.get("updated_at", ""),
    } for j in r.json().get("jobs", [])]


def fetch_lever(c: dict) -> list[dict]:
    r = sess().get(f"https://api.lever.co/v0/postings/{c['site']}?mode=json", timeout=TIMEOUT)
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
        r = sess().get(f"https://api.smartrecruiters.com/v1/companies/{cid}/postings",
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
    tld = c.get("tld", "de")
    r = sess().get(f"https://{sub}.jobs.personio.{tld}/xml", timeout=TIMEOUT)
    r.raise_for_status()
    root = ET.fromstring(r.content)
    out = []
    for pos in root.iter("position"):
        pid = (pos.findtext("id") or "").strip()
        out.append({
            "id": pid,
            "title": (pos.findtext("name") or "").strip(),
            "location": (pos.findtext("office") or "").strip(),
            "url": f"https://{sub}.jobs.personio.{tld}/job/{pid}",
            "posted": (pos.findtext("createdAt") or "")[:10],
        })
    return out


def fetch_html(c: dict) -> list[dict]:
    """Generic list page. Options: item (CSS), title (CSS | self | lines), link, location (CSS),
    location_text, page_param + max_pages (for paged lists), drop (words to ignore in lines mode).
    title: lines -> the first text line of the item is the title, the remaining lines the location."""
    out: dict[str, dict] = {}
    pages = int(c.get("max_pages", 1)) if c.get("page_param") else 1
    empty = 0
    for page in range(int(c.get("start_page", 1)), int(c.get("start_page", 1)) + pages):
        params = {c["page_param"]: page} if c.get("page_param") else None
        r = sess().get(c["url"], params=params, timeout=TIMEOUT)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        added = 0
        for item in soup.select(c["item"]):
            a = item.select_one(c["link"]) if c.get("link") else (item if item.name == "a" else item.find("a"))
            if not a or not a.get("href"):
                continue
            url = urljoin(c["url"], a["href"])
            if url in out:
                continue
            if c.get("title") == "lines":
                drop = {w.lower() for w in c.get("drop", [])} | {"new", "read more"}
                lines = [x for x in item.get_text("\n", strip=True).split("\n") if x.lower() not in drop]
                if not lines:
                    continue
                title, loc_txt = lines[0], " | ".join(lines[1:])
            else:
                t = item.select_one(c["title"]) if c.get("title") and c["title"] != "self" else item
                if not t:
                    continue
                title = t.get_text(" ", strip=True)
                locs = item.select(c["location"]) if c.get("location") else []
                loc = locs[int(c.get("location_index", 0))] if len(locs) > abs(int(c.get("location_index", 0))) - (1 if int(c.get("location_index", 0)) < 0 else 0) else None
                loc_txt = loc.get_text(" ", strip=True) if loc else c.get("location_text", "")
            if c.get("strip_prefix") and title.startswith(c["strip_prefix"]):
                title = title[len(c["strip_prefix"]):]
            out[url] = {
                "id": url,
                "title": re.sub(r"\s*\((?:DE|EN)\)\s*$", "", title),
                "location": loc_txt,
                "url": url,
                "posted": "",
            }
            if c.get("date") and item.select_one(c["date"]):
                out[url]["posted"] = sf_date(item.select_one(c["date"]).get_text(" ", strip=True))
            added += 1
        empty = 0 if added else empty + 1
        if c.get("page_param") and empty >= 3:  # three pages in a row with nothing new = end of the list
            break
    return list(out.values())


def fetch_onlyfy(c: dict) -> list[dict]:
    """Career pages run on onlyfy (Stepstone), e.g. https://eumetsat.onlyfy.jobs"""
    base = f"https://{c['subdomain']}.onlyfy.jobs"
    out: dict[str, dict] = {}
    for page in range(1, int(c.get("max_pages", 15)) + 1):
        r = sess().get(f"{base}/en", params={"page": page} if page > 1 else None, timeout=TIMEOUT)
        r.raise_for_status()
        cards = BeautifulSoup(r.text, "html.parser").select('a[data-testid="job-card"]')
        added = 0
        for a in cards:
            jid = a.get("href", "").rstrip("/").rsplit("/", 1)[-1]
            if not jid or jid in out:
                continue
            t = a.select_one('[data-testid="job-title"]')
            info = a.select_one('[data-testid="job-more-info"]')
            out[jid] = {
                "id": jid,
                "title": (t.get_text(" ", strip=True) if t else a.get("aria-label", "")).strip(),
                "location": (info.get_text(" ", strip=True).split("|")[0].strip() if info else ""),
                "url": urljoin(base, a["href"]),
                "posted": "",
            }
            added += 1
        if not added:
            break
    return list(out.values())


def sf_date(txt: str) -> str:
    """'2026-10-09' or 'Oct 9, 2026' or '09.10.2026' -> '2026-10-09' ('' if unknown)."""
    txt = txt.strip()
    for fmt in ("%Y-%m-%d", "%b %d, %Y", "%d.%m.%Y", "%d %b %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(txt[:10] if fmt == "%Y-%m-%d" else txt, fmt).date().isoformat()
        except ValueError:
            continue
    return ""


def fetch_successfactors(c: dict) -> list[dict]:
    """SAP SuccessFactors career sites (e.g. https://jobs.esa.int): /search/ lists 25 jobs per page."""
    base = c["url"].rstrip("/")
    out: dict[str, dict] = {}
    for page in range(int(c.get("max_pages", 20))):
        r = sess().get(f"{base}/search/", params={"q": c.get("search", ""), "startrow": page * 25,
                                                  "sortColumn": "referencedate", "sortDirection": "desc"}, timeout=TIMEOUT)
        r.raise_for_status()
        tiles = BeautifulSoup(r.text, "html.parser").select("li.job-tile, tr.data-row")
        added = 0
        for tile in tiles:
            a = tile.select_one("a.jobTitle-link")
            if not a or not a.get("href"):
                continue
            url = urljoin(base + "/", a["href"].split("?")[0])
            if url in out:
                continue
            loc = tile.select_one('[id$="section-location-value"], [id$="section-multilocation-value"], .jobLocation, [class*=location] .section-value')
            date = tile.select_one('[id$="section-date-value"], .jobDate, [class*=date] .section-value')
            out[url] = {
                "id": url,
                "title": a.get_text(" ", strip=True),
                "location": loc.get_text(" ", strip=True) if loc else "",
                "url": url,
                "posted": sf_date(date.get_text(" ", strip=True)) if date else "",
            }
            added += 1
        if added < 1 or len(tiles) < 5:
            break
    return list(out.values())


def fetch_recruitee(c: dict) -> list[dict]:
    r = sess().get(f"https://{c['subdomain']}.recruitee.com/api/offers/", timeout=TIMEOUT)
    r.raise_for_status()
    return [{
        "id": str(o["id"]),
        "title": (o.get("title") or "").strip(),
        "location": ", ".join(x for x in (o.get("city"), o.get("country")) if x),
        "url": o.get("careers_url", ""),
        "posted": (o.get("published_at") or "")[:10],
    } for o in r.json().get("offers", [])]


def fetch_workable(c: dict) -> list[dict]:
    r = sess().get(f"https://apply.workable.com/api/v1/widget/accounts/{c['account']}", timeout=TIMEOUT)
    r.raise_for_status()
    return [{
        "id": j.get("shortcode") or j.get("url", ""),
        "title": (j.get("title") or "").strip(),
        "location": ", ".join(x for x in (j.get("city"), j.get("country")) if x),
        "url": j.get("url", ""),
        "posted": (j.get("created_at") or "")[:10],
    } for j in r.json().get("jobs", [])]


def fetch_ashby(c: dict) -> list[dict]:
    r = sess().get(f"https://api.ashbyhq.com/posting-api/job-board/{c['board']}", timeout=TIMEOUT)
    r.raise_for_status()
    return [{
        "id": j["id"],
        "title": (j.get("title") or "").strip(),
        "location": j.get("location") or "",
        "url": j.get("jobUrl", ""),
        "posted": (j.get("publishedAt") or "")[:10],
    } for j in r.json().get("jobs", [])]


def fetch_csod(c: dict) -> list[dict]:
    """Cornerstone career sites (e.g. https://career-ohb.csod.com): the public home page embeds an
    anonymous token that the site's own job search call uses."""
    corp, site = c["corp"], int(c.get("site", 1))
    home = f"https://{corp}.csod.com/ux/ats/careersite/{site}/home?c={corp}"
    h = sess().get(home, timeout=TIMEOUT)
    h.raise_for_status()
    tok = re.search(r'"token"\s*:\s*"([^"]+)"', h.text)
    api = re.search(r"https://[a-z0-9-]+\.api\.csod\.com", h.text)
    if not tok:
        raise RuntimeError("csod: no anonymous token on career page")
    base = api.group(0) if api else "https://eu-fra.api.csod.com"
    hdr = {"Authorization": "Bearer " + tok.group(1), "Content-Type": "application/json"}
    out: dict[str, dict] = {}
    total = None
    for page in range(1, int(c.get("max_pages", 30)) + 1):
        body = {"careerSiteId": site, "careerSitePageId": site, "pageNumber": page, "pageSize": 25,
                "cultureId": 4, "searchText": c.get("search", ""), "cultureName": "de-DE", "states": [],
                "countryCodes": [], "cities": [], "placeID": "", "radius": None, "postingsWithinDays": None,
                "customFieldCheckboxKeys": [], "customFieldDropdowns": [], "customFieldRadios": []}
        r = sess().post(f"{base}/rec-job-search/external/jobs", json=body, headers=hdr, timeout=TIMEOUT)
        r.raise_for_status()
        d = r.json().get("data") or {}
        total = d.get("totalCount", total)
        reqs = d.get("requisitions") or []
        for q in reqs:
            rid = str(q["requisitionId"])
            locs = q.get("locations") or []
            loc = ", ".join(dict.fromkeys(
                x for l in locs for x in (l.get("city"), l.get("country")) if x))
            out[rid] = {
                "id": rid,
                "title": (q.get("displayJobTitle") or "").strip(),
                "location": loc,
                "url": f"https://{corp}.csod.com/ux/ats/careersite/{site}/home/requisition/{rid}?c={corp}",
                "posted": sf_date(q.get("postingEffectiveDate") or ""),
            }
        if not reqs or (total is not None and len(out) >= total):
            break
    return list(out.values())


ADAPTERS = {
    "csod": fetch_csod,
    "arbeitsagentur": fetch_arbeitsagentur,
    "workday": fetch_workday,
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "smartrecruiters": fetch_smartrecruiters,
    "personio": fetch_personio,
    "html": fetch_html,
    "onlyfy": fetch_onlyfy,
    "successfactors": fetch_successfactors,
    "recruitee": fetch_recruitee,
    "workable": fetch_workable,
    "ashby": fetch_ashby,
}


# ----------------------------------------------------------------- filters ---

def title_has(term: str, title: str) -> bool:
    """Keyword match on a lower-case title. Short terms (<= 4 letters) must start a word,
    so 'lean' matches 'Lean Manager' but not 'Clean Room Engineer'."""
    t = term.lower()
    if len(t) <= 4:
        return re.search(r"(?<![a-zäöüß])" + re.escape(t), title) is not None
    return t in title


def passes_filters(job: dict, company: dict, global_filters: dict) -> bool:
    def pick(key):
        return company.get(key, global_filters.get(key) or [])

    title = job["title"].lower()
    loc = job["location"].lower()
    include, exclude, locations = pick("include"), pick("exclude"), pick("locations")
    if include and not any(title_has(k, title) for k in include):
        return False
    if exclude and any(title_has(k, title) for k in exclude):
        return False
    if locations == "de":  # "Germany": country words, or one of the German places listed under filters.de_locations
        if loc and not (re.match(r"\d+ (standorte|locations)", loc)  # Workday: several places, not named
                        or re.search(r"(?<![a-zäöü])(de|deutschland|germany)(?![a-zäöü])", loc)
                        or any(k.lower() in loc for k in global_filters.get("de_locations", []))):
            return False
    elif locations and loc and not any(k.lower() in loc for k in locations):
        return False
    return True


# ------------------------------------------------------------------- merge ---

def merge(old: dict, fetched: dict[str, list[dict]], errors: dict[str, str],
          cfg: dict, now: str, raw: dict[str, int] | None = None) -> dict:
    """Pure function: old state + fetch results -> new state (no 'checked' timestamps)."""
    retention = timedelta(days=int(cfg.get("closed_retention_days", 14)))
    now_dt = datetime.fromisoformat(now)
    old_jobs = {(j["company"], j["id"]): j for j in old.get("jobs", [])}
    baselined = set(old.get("baselined", []))
    known_companies = {j["company"] for j in old.get("jobs", [])} | baselined
    configured = {c["name"] for c in cfg["companies"]}
    result: dict[tuple, dict] = {}

    # Companies that failed, or were not part of this run, keep their previous jobs.
    for key, job in old_jobs.items():
        if job["company"] not in fetched and job["company"] in configured:
            result[key] = job

    for company, jobs in fetched.items():
        is_baseline = company not in known_companies
        seen = set()
        for j in jobs:
            key = (company, j["id"])
            seen.add(key)
            prev = old_jobs.get(key)
            if prev:
                entry = {**prev, **{k: j[k] for k in ("title", "location", "url", "posted", "match", "src") if k in j}}
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

    jobs_sorted = sorted(result.values(),
                         key=lambda j: (j["first_seen"], j.get("posted", ""), j["title"]),
                         reverse=True)
    status = dict(old.get("companies", {}))
    for name, jobs in fetched.items():
        status[name] = {"ok": True, "count": len(jobs)}
        if raw and name in raw:
            status[name]["raw"] = raw[name]  # jobs the career site returned before the profile filters
    for name, msg in errors.items():
        status[name] = {"ok": False, "error": msg[:200],
                        "count": status.get(name, {}).get("count", 0)}
    status = {k: v for k, v in status.items() if k in configured}
    return {"new_window_hours": int(cfg.get("new_window_hours", 48)),
            "baselined": sorted(baselined & configured),
            "companies": status, "jobs": jobs_sorted}


RAW: dict[str, int] = {}


def run_company(c: dict, global_filters: dict, core: list[str]) -> tuple[str, list[dict] | None, str | None]:
    name, ctype = c["name"], c.get("type")
    adapter = ADAPTERS.get(ctype)
    if not adapter:
        return name, None, f"unknown type '{ctype}'"
    try:
        found = [j for j in adapter(c) if j["id"] and j["title"]]
        RAW[name] = len(found)
        jobs = [j for j in found if passes_filters(j, c, global_filters)]
        for j in jobs:  # 2 = strong match for your profile ("core" words), 1 = related
            j["match"] = 2 if any(title_has(k, j["title"].lower()) for k in core) else 1
            j["src"] = "agency" if ctype == "arbeitsagentur" else "site"  # own career site vs. job-agency feed
        return name, jobs, None
    except Exception as exc:  # one broken company must not stop the others
        return name, None, f"{type(exc).__name__}: {exc}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tier", default="all", choices=["fast", "slow", "all"])
    args = ap.parse_args()

    cfg = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    companies = cfg.get("companies") or []
    if not companies:
        print("No companies configured in companies.yml")
        return 1
    global_filters = cfg.get("filters") or {}
    profile = cfg.get("profile") or {}
    core = profile.get("core") or []
    if "include" not in global_filters and (core or profile.get("related")):
        # a job must contain a core or related profile word in its title
        global_filters = {**global_filters, "include": core + (profile.get("related") or [])}
    todo =[c for c in companies if args.tier == "all" or c.get("tier", "fast") == args.tier]
    print(f"Tier '{args.tier}': {len(todo)} of {len(companies)} companies")

    fetched: dict[str, list[dict]] = {}
    errors: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=int(cfg.get("workers", 4))) as pool:
        for name, jobs, err in pool.map(lambda c: run_company(c, global_filters, core), todo):
            if err:
                errors[name] = err
                print(f"[{name}] FAILED: {err}", file=sys.stderr)
            else:
                fetched[name] = jobs
                print(f"[{name}] {len(jobs)} jobs")

    old = json.loads(OUT_PATH.read_text(encoding="utf-8")) if OUT_PATH.exists() else {}
    now = now_iso()
    old_core = {k: v for k, v in old.items() if k != "updated"}
    new_state = merge(old_core, fetched, errors, cfg, now, RAW)

    if old and old_core == new_state:
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
