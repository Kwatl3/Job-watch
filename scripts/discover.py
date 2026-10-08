#!/usr/bin/env python3
"""Find out which career system each company in companies.yml uses.

Writes docs/discovery.json. For every company it
  1. probes the public job APIs of the common career systems with guessed company names, and
  2. searches the web for the company's career page and looks for career-system links in it.
Run it from the Actions tab (workflow "Discover career sites"). It is not part of the 30-minute job fetch.
"""
from __future__ import annotations

import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

import requests
import yaml
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "discovery.json"
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
      "Accept-Language": "de,en;q=0.8"}
T = 12

LEGAL = r"\b(gmbh|ag|se|kg|kgaa|ohg|mbh|eg|ug|sa|srl|spa|ab|as|plc|ltd|co|inc|group|gruppe|holding|deutschland|germany|international)\b"
SKIP_HOSTS = ("duckduckgo.", "bing.", "google.", "wikipedia.", "linkedin.", "xing.", "kununu.", "indeed.", "stepstone.",
              "glassdoor.", "facebook.", "youtube.", "instagram.", "twitter.", "x.com", "get-in-", "jobvector", "yourfirm",
              "monster.", "jobware", "stellenanzeigen", "arbeitsagentur", "absolventa", "studysmarter", "northdata",
              "wlw.", "firmenwissen", "meinestadt", "ausbildung.de", "azubi", "jooble", "adzuna", "talent.com")

# career-system signatures found in page HTML
SIGS = {
    "workday": r"https?://([a-z0-9-]+)\.wd(\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([A-Za-z0-9_-]+)",
    "smartrecruiters": r"(?:jobs|careers)\.smartrecruiters\.com/([A-Za-z0-9_-]+)",
    "personio": r"https?://([a-z0-9-]+)\.jobs\.personio\.(?:de|com)",
    "greenhouse": r"(?:boards|job-boards)\.(?:eu\.)?greenhouse\.io/([A-Za-z0-9_-]+)",
    "lever": r"jobs\.(?:eu\.)?lever\.co/([A-Za-z0-9_-]+)",
    "recruitee": r"https?://([a-z0-9-]+)\.recruitee\.com",
    "workable": r"apply\.workable\.com/([A-Za-z0-9_-]+)",
    "ashby": r"jobs\.ashbyhq\.com/([A-Za-z0-9_.-]+)",
    "join": r"join\.com/companies/([A-Za-z0-9_-]+)",
    "softgarden": r"https?://([a-z0-9-]+)\.softgarden\.io",
    "successfactors": r"(?:career|jobs)[a-z0-9-]*\.successfactors\.(?:eu|com)|/talentcommunity/|sf-career",
    "oracle": r"https?://([a-z0-9-]+)\.fa\.[a-z0-9-]+\.oraclecloud\.com",
    "taleo": r"https?://([a-z0-9-]+)\.taleo\.net",
    "icims": r"https?://([a-z0-9-]+)\.icims\.com",
    "teamtailor": r"https?://([a-z0-9-]+)\.teamtailor\.com",
    "rexx": r"https?://([a-z0-9-]+)\.rexx-systems\.com",
    "dvinci": r"https?://([a-z0-9-]+)\.dvinci-hr\.com|d-vinci",
    "umantis": r"https?://([a-z0-9-]+)\.abacus-hr\.ch|umantis\.com",
    "onlyfy": r"onlyfy\.jobs|prescreen\.io",
    "persy": r"persy\.jobs",
    "eightfold": r"https?://([a-z0-9-]+)\.eightfold\.ai",
    "phenom": r"phenompeople|phenom\.com",
    "avature": r"https?://([a-z0-9-]+)\.avature\.net",
    "jobvite": r"jobs\.jobvite\.com/([A-Za-z0-9_-]+)",
    "bamboohr": r"https?://([a-z0-9-]+)\.bamboohr\.com",
}


def slugs(name: str, employer: str) -> list[str]:
    out: list[str] = []
    for base in (employer, name):
        s = re.sub(r"\(.*?\)", " ", base.lower())
        s = s.replace("&", " and ").replace("ä", "ae").replace("ö", "oe").replace("ü", "ue").replace("ß", "ss")
        s = re.sub(LEGAL, " ", s)
        words = re.findall(r"[a-z0-9]+", s)
        if not words:
            continue
        for cand in ("".join(words), "-".join(words), "_".join(words), words[0], "".join(words[:2]), "-".join(words[:2])):
            if len(cand) >= 3 and cand not in out:
                out.append(cand)
    return out[:4]


def get(url: str, **kw):
    try:
        return requests.get(url, headers=UA, timeout=T, allow_redirects=kw.pop("allow_redirects", True), **kw)
    except Exception:
        return None


def probe_apis(slug_list: list[str]) -> list[dict]:
    hits = []
    for s in slug_list:
        r = get(f"https://api.smartrecruiters.com/v1/companies/{s}/postings?limit=1")
        if r is not None and r.status_code == 200:
            try:
                n = r.json().get("totalFound", 0)
            except Exception:
                n = 0
            if n:
                hits.append({"type": "smartrecruiters", "company_id": s, "jobs": n})
        r = get(f"https://boards-api.greenhouse.io/v1/boards/{s}/jobs")
        if r is not None and r.status_code == 200:
            try:
                n = len(r.json().get("jobs", []))
            except Exception:
                n = 0
            if n:
                hits.append({"type": "greenhouse", "board": s, "jobs": n})
        r = get(f"https://api.lever.co/v0/postings/{s}?mode=json")
        if r is not None and r.status_code == 200:
            try:
                n = len(r.json())
            except Exception:
                n = 0
            if n:
                hits.append({"type": "lever", "site": s, "jobs": n})
        for tld in ("de", "com"):
            r = get(f"https://{s}.jobs.personio.{tld}/xml")
            if r is not None and r.status_code == 200 and "<position" in r.text:
                hits.append({"type": "personio", "subdomain": s, "tld": tld, "jobs": r.text.count("<position>")})
                break
        r = get(f"https://{s}.recruitee.com/api/offers/")
        if r is not None and r.status_code == 200:
            try:
                n = len(r.json().get("offers", []))
            except Exception:
                n = 0
            if n:
                hits.append({"type": "recruitee", "subdomain": s, "jobs": n})
        r = get(f"https://apply.workable.com/api/v1/widget/accounts/{s}")
        if r is not None and r.status_code == 200:
            try:
                n = len(r.json().get("jobs", []))
            except Exception:
                n = 0
            if n:
                hits.append({"type": "workable", "account": s, "jobs": n})
        r = get(f"https://api.ashbyhq.com/posting-api/job-board/{s}")
        if r is not None and r.status_code == 200:
            try:
                n = len(r.json().get("jobs", []))
            except Exception:
                n = 0
            if n:
                hits.append({"type": "ashby", "board": s, "jobs": n})
        for wd in (1, 3, 5, 103):
            r = get(f"https://{s}.wd{wd}.myworkdayjobs.com/", allow_redirects=True)
            if r is not None and r.status_code == 200 and "myworkdayjobs" in r.url:
                m = re.search(r"myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([A-Za-z0-9_-]+)", r.url)
                hits.append({"type": "workday", "host": f"{s}.wd{wd}.myworkdayjobs.com", "tenant": s,
                             "site": m.group(1) if m else "", "url": r.url[:150]})
                break
        if hits:
            break
    return hits


def search(q: str) -> list[str]:
    urls: list[str] = []
    r = None
    try:
        r = requests.post("https://html.duckduckgo.com/html/", data={"q": q}, headers=UA, timeout=T)
    except Exception:
        pass
    if r is not None and r.status_code == 200:
        soup = BeautifulSoup(r.text, "html.parser")
        for a in soup.select("a.result__a"):
            href = a.get("href", "")
            if "uddg=" in href:
                href = unquote(parse_qs(urlparse(href).query).get("uddg", [""])[0])
            if href.startswith("http"):
                urls.append(href)
    if not urls:
        r = get("https://www.bing.com/search?q=" + quote(q) + "&setlang=de")
        if r is not None and r.status_code == 200:
            soup = BeautifulSoup(r.text, "html.parser")
            for a in soup.select("li.b_algo h2 a"):
                if a.get("href", "").startswith("http"):
                    urls.append(a["href"])
    keep = [u for u in urls if not any(h in urlparse(u).netloc for h in SKIP_HOSTS)]
    return keep[:4]


def detect(html: str) -> dict:
    found: dict[str, list[str]] = {}
    for kind, rx in SIGS.items():
        ms = list(re.finditer(rx, html, re.I))
        if ms:
            found[kind] = sorted({m.group(0)[:140] for m in ms})[:3]
    return found


def web_probe(name: str) -> dict:
    res = {"pages": []}
    for url in search(f"{name} Karriere Stellenangebote"):
        r = get(url)
        if r is None or r.status_code != 200:
            continue
        page = {"url": r.url[:160], "ats": detect(r.text)}
        # one hop: follow a link that looks like the jobs page
        if not page["ats"]:
            soup = BeautifulSoup(r.text, "html.parser")
            for a in soup.find_all("a", href=True):
                label = (a.get_text(" ", strip=True) + " " + a["href"]).lower()
                if re.search(r"stellenangebote|stellenmarkt|job-?suche|jobsuche|open positions|current openings|search jobs|jobs|karriere|careers", label):
                    nxt = requests.compat.urljoin(r.url, a["href"])
                    if nxt.startswith("http") and nxt != r.url:
                        r2 = get(nxt)
                        if r2 is not None and r2.status_code == 200:
                            page["hop"] = r2.url[:160]
                            page["ats"] = detect(r2.text) or detect(r.text)
                            break
        res["pages"].append(page)
        if page["ats"]:
            break
        time.sleep(0.3)
    return res


def run(c: dict) -> tuple[str, dict]:
    name = c["name"]
    emp = c.get("employer", name)
    out = {"slugs": slugs(name, emp)}
    try:
        out["api"] = probe_apis(out["slugs"])
    except Exception as exc:
        out["api_error"] = str(exc)[:100]
    if not out.get("api"):
        try:
            out["web"] = web_probe(name)
        except Exception as exc:
            out["web_error"] = str(exc)[:100]
    return name, out


def main() -> int:
    cfg = yaml.safe_load((ROOT / "companies.yml").read_text(encoding="utf-8"))
    skip = re.compile(r"klinik|kranken|sparkasse|volksbank|bank|versicher|stadtwerke|hospital|diakonie|pflege|reinigung|"
                      r"gebäude|gebaeude|möbel|moebel|gastronom|drogerie|bauhaus|edeka|intersport|euronics|engelhorn|"
                      r"breuninger|europa-park|dussmann|logistik|logistics|verlag|medien|zeitung|presse|bahn|straßenbahn|"
                      r"verkehr|recruit|personal|ravensburger|trigema|hugo boss|ritter|schwabe|weleda|takeda|teva|roche", re.I)
    comps = [c for c in cfg["companies"] if c.get("type") == "arbeitsagentur" and not skip.search(c["name"])]
    only = sys.argv[1] if len(sys.argv) > 1 else ""
    if only:
        comps = [c for c in comps if only.lower() in c["name"].lower()]
    print(f"Discovering {len(comps)} companies")
    res = {}
    with ThreadPoolExecutor(max_workers=6) as pool:
        for name, out in pool.map(run, comps):
            res[name] = out
            kinds = [h["type"] for h in out.get("api", [])] or [k for p in out.get("web", {}).get("pages", []) for k in p["ats"]]
            print(f"{name}: {', '.join(kinds) or '-'}")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(res, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
