#!/usr/bin/env python3
"""
seed_scout.py - find freshly seed-funded startups that have just started hiring
for Applied AI / Machine Learning roles.

Standard library only (Python 3.9+). No API keys.

How it works
------------
1. FUNDING NEWS   Reads RSS feeds (TechCrunch, EU-Startups, Silicon Canals, ...,
                  plus Google News searches) and keeps headlines about seed /
                  pre-seed rounds. The company name is pulled out of the headline.
2. WATCHLIST      Each company is remembered in a state file for --watch-days
                  (default 180), because hiring usually starts weeks after the
                  announcement. Every run re-checks the whole watchlist.
3. JOB BOARDS     For each company, guesses its job-board slug and asks the public
                  JSON APIs of Greenhouse, Lever, Ashby, Workable and Recruitee.
4. FILTER         Keeps roles whose title matches Applied AI / ML keywords, on
                  boards with at most --max-open-roles openings ("just started").
5. EXTRA SOURCES  Hacker News "Who is hiring?" posts that mention a seed round.
                  (YC company-level leads exist behind --include-yc, off by default.)
6. OUTPUT         CSV sorted with new-since-last-run rows first.

Usage
-----
    python3 seed_scout.py                 # full run -> seed_jobs.csv
    python3 seed_scout.py --core-only --json docs/jobs.json
    DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/... python3 seed_scout.py
    python3 seed_scout.py --selftest      # offline tests, no network

Run it daily (cron / Task Scheduler): RSS feeds only hold the latest ~20 items,
so the watchlist is built up over time.

Known limits (read these)
-------------------------
* Company names come from headline heuristics and board slugs are guesses, so
  expect some misses and the odd wrong-company match. Check `board_match`
  ("exact" vs "partial") and the URL. Fix misses via SLUG_OVERRIDES below.
* Companies with a careers page on their own site / Notion / LinkedIn only are
  not found by step 3.
* YC rows are company-level leads (the role is not verified).
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import html
import json
import os
import re
import socket
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from email.utils import parsedate_to_datetime

# ----------------------------------------------------------------------------
# CONFIG - edit freely
# ----------------------------------------------------------------------------

USER_AGENT = "seed-scout/1.0 (personal job-search script)"

# (name, url, is_google_news). Feeds that fail are skipped with a warning.
FEEDS = [
    ("TechCrunch Startups", "https://techcrunch.com/category/startups/feed/", False),
    ("TechCrunch Venture", "https://techcrunch.com/category/venture/feed/", False),
    ("EU-Startups", "https://www.eu-startups.com/feed/", False),
    ("Silicon Canals", "https://siliconcanals.com/feed/", False),
    ("Tech.eu", "https://tech.eu/feed/", False),
    ("UKTN", "https://www.uktech.news/feed", False),
    ("Tech Funding News", "https://techfundingnews.com/feed/", False),
]
GOOGLE_NEWS_QUERIES = [
    '"seed round" AI startup raises when:14d',
    '"pre-seed" AI startup raises when:14d',
    '"seed" AI startup Amsterdam OR Netherlands raises when:30d',
    '"seed" AI startup London raises when:14d',
]
for _q in GOOGLE_NEWS_QUERIES:
    FEEDS.append((
        "Google News: " + _q,
        "https://news.google.com/rss/search?" + urllib.parse.urlencode(
            {"q": _q, "hl": "en-GB", "gl": "GB", "ceid": "GB:en"}),
        True,
    ))

# Role title patterns. "core" = what you asked for; "adjacent" = titles that at
# a seed-stage AI company usually mean the same job. Use --core-only to drop them.
CORE_ROLE = re.compile(
    r"\b(applied ai|applied ml|applied scientist|ai engineer|ai/ml|ml/ai|ai & ml|"
    r"machine[- ]learning|ml engineer|ml ops|mlops|ml scientist|ml researcher|"
    r"llm|genai|gen ai|generative ai|deep learning|nlp|computer vision|"
    r"ai research|ai scientist|ai developer|ai software engineer|agent engineer)\b",
    re.I)
ADJACENT_ROLE = re.compile(
    r"\b(founding engineer|founding .*engineer|forward[- ]deployed|"
    r"member of technical staff|research engineer|research scientist|data scientist)\b",
    re.I)

# PERSONALISATION: graduate / junior roles only. A role is kept when its TITLE
# says graduate/junior/entry-level (JUNIOR) or its DESCRIPTION clearly invites
# graduates (GRAD_SIGNAL), and it is not senior/mid/lead (SENIOR), not an
# internship (INTERN), and doesn't ask for more than MAX_YEARS_EXP years.
# Use --any-level to see everything.
SENIOR = re.compile(r"\b(senior|sr|staff|principal|lead|head|director|vp|manager|chief|architect|"
                    r"experienced|expert|mid|mid[- ]level|ii|iii|iv)\b", re.I)
JUNIOR = re.compile(r"\b(junior|jr|graduate|graduates|grad|new grad|entry[- ]level|"
                    r"early[- ]career|associate|trainee|2027)\b", re.I)
INTERN = re.compile(r"\b(intern|interns|internship|placement|working student|werkstudent|stage|"
                    r"apprentice|apprenticeship|thesis)\b", re.I)
# Bare "junior" is deliberately absent: "mentor junior engineers" describes a senior role.
GRAD_SIGNAL = re.compile(r"\b(new grad(?:uate)?s?|recent graduates?|graduate (?:role|scheme|programme|program|position)|"
                         r"(?:fresh|recent) grads?|entry[- ]level|early[- ]career|class of 2027|"
                         r"2027 (?:start|grad\w*)|no (?:prior |previous )?experience (?:required|needed|necessary))\b", re.I)
MAX_YEARS_EXP = 2
YEARS = re.compile(r"(\d{1,2})\s*(?:\+|plus)?\s*(?:(?:-|–|to)\s*\d{1,2}\s*\+?\s*)?(?:years?|yrs?)\b"
                   r"[^.\n]{0,60}?\bexperience", re.I)

# PERSONALISATION: only the Netherlands (tier 0) and the UK (tier 1). Jobs with
# a known location matching neither are dropped ("Remote, Europe" included,
# "Remote (UK)" kept). Use --any-location to disable.
LOCATION_TIERS = [
    re.compile(r"\b(netherlands|nederland|amsterdam|rotterdam|utrecht|eindhoven|delft|"
               r"the hague|den haag|leiden|groningen|nl)\b", re.I),
    re.compile(r"\b(london|united kingdom|uk|gb|great britain|england|scotland|wales|oxford|"
               r"cambridge(?!,?\s*(?:ma|mass)\b)|manchester|edinburgh|bristol|glasgow|leeds|birmingham)\b", re.I),
]

VISA = re.compile(r"\b(visa|sponsorship|sponsor|relocation)\b", re.I)

# If a company's board isn't found by guessing, pin it here:
#   "company name (lowercase)": ("ashby" | "greenhouse" | "lever" | "lever_eu" |
#                                "workable" | "recruitee", "slug")
SLUG_OVERRIDES: dict[str, tuple[str, str]] = {}

# ----------------------------------------------------------------------------
# HTTP
# ----------------------------------------------------------------------------


def http_get(url: str, timeout: int = 20, retries: int = 2) -> bytes | None:
    """GET url. Returns body, or None on any failure (never raises)."""
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            if e.code == 429 or e.code >= 500:
                time.sleep(2 * (attempt + 1))
                continue
            return None  # 404 etc: board/feed doesn't exist
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError, ValueError):
            time.sleep(1 + attempt)
    return None


def get_json(url: str):
    body = http_get(url)
    if not body:
        return None
    try:
        return json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return None


# ----------------------------------------------------------------------------
# Small helpers
# ----------------------------------------------------------------------------


def strip_html(s) -> str:
    if not s:
        return ""
    s = html.unescape(str(s))            # Greenhouse double-escapes its HTML
    s = re.sub(r"<(br|/p|/div|/li)[^>]*>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    return re.sub(r"[ \t\r\f\v]+", " ", s).strip()


def to_date(v) -> str:
    """Best-effort conversion of an API date value to YYYY-MM-DD ('' if unknown)."""
    if v is None or v == "":
        return ""
    if isinstance(v, (int, float)):
        try:
            secs = v / 1000 if v > 1e11 else v      # ms vs s epoch
            return dt.datetime.fromtimestamp(secs, dt.timezone.utc).date().isoformat()
        except (ValueError, OverflowError, OSError):
            return ""
    m = re.match(r"\d{4}-\d{2}-\d{2}", str(v))
    return m.group(0) if m else ""


def location_tier(loc: str) -> int:
    for i, rx in enumerate(LOCATION_TIERS):
        if rx.search(loc or ""):
            return i
    return len(LOCATION_TIERS)


def role_match(title: str) -> str:
    if CORE_ROLE.search(title or ""):
        return "core"
    if ADJACENT_ROLE.search(title or ""):
        return "adjacent"
    return ""


def level_of(title: str) -> str:
    if SENIOR.search(title or ""):
        return "senior"
    if JUNIOR.search(title or ""):
        return "junior"
    return "unspecified"


def is_grad_role(title: str, desc: str) -> tuple[bool, str]:
    """(keep?, grad_signal) under the graduate/junior-only rule."""
    level = level_of(title)
    if level == "senior" or INTERN.search(title or ""):
        return False, ""
    years = min_years_required(desc)
    if years is not None and years > MAX_YEARS_EXP:
        return False, ""
    if level == "junior":
        return True, "title"
    if GRAD_SIGNAL.search(desc or ""):
        return True, "description"
    return False, ""


def min_years_required(desc: str) -> int | None:
    """Largest 'N years ... experience' requirement found in a description."""
    vals = [int(m.group(1)) for m in YEARS.finditer(desc or "")]
    vals = [v for v in vals if v <= 15]
    return max(vals) if vals else None


# ----------------------------------------------------------------------------
# 1. Funding news -> company names
# ----------------------------------------------------------------------------

SEED = re.compile(r"\b(pre[- ]?seed|seed)\b", re.I)
LATER_ROUND = re.compile(r"\bseries\s+[a-z]\b|\bipo\b|\bgrowth round\b", re.I)
AMOUNT = re.compile(r"(?:US)?[$€£]\s?\d+(?:[.,]\d+)?\s?(?:k|m|mn|million|bn|billion)?\b", re.I)
VERB = re.compile(
    r"\b(raises|raised|secures|secured|lands|landed|bags|bagged|closes|closed|nabs|"
    r"snags|scores|grabs|nets|banks|gets|receives|attracts|collects|picks up|pulls in|"
    r"hauls in|rakes in|emerges from stealth|comes out of stealth|launches with|"
    r"launches out of stealth|announces|completes|locks in|snaps up|wins)\b", re.I)

# Words that describe a company rather than name it.
STOP = set("""
startup startups start-up scaleup scale-up company firm platform maker developer
provider spinout spin-out unicorn stealth new former ex the a an and how why what
meet exclusive breaking this these after with as inside
ai a.i. ml fintech healthtech medtech biotech deeptech deep tech climate climatetech
cleantech legaltech legal proptech insurtech edtech agritech agtech foodtech hr hrtech
saas b2b robotics cybersecurity security quantum defence defense space spacetech
crypto web3 software hardware data voice agentic generative genai health bio energy
dutch british uk german french swedish danish finnish norwegian spanish italian irish
belgian swiss austrian polish portuguese estonian european american us israeli indian
canadian nordic london amsterdam berlin paris stockholm munich cambridge oxford
rotterdam delft eindhoven utrecht zurich dublin barcelona madrid helsinki copenhagen
oslo lisbon tallinn edinburgh bristol manchester nyc sf
""".split())
# STOP words that may still be the *last* word of a name ("Foo AI", "Foo Labs").
NAME_SUFFIX_OK = set("ai labs lab robotics security data space quantum tech health bio "
                     "energy systems technologies".split())
MODIFIER_END = ("-based", "-backed", "-born", "-founded", "-led", "-headquartered",
                "-focused", "-native", "-powered", "-first")


def _namelike(tok: str) -> bool:
    return bool(tok) and (tok[0].isupper() or tok[0].isdigit() or any(c.isupper() for c in tok[1:])
                          or re.fullmatch(r"[a-z0-9]+\.[a-z]{2,4}", tok) is not None)


def _name_from_segment(seg: str) -> str | None:
    toks = [t.strip("\"'“”‘’()[]") for t in seg.split()]
    toks = [t for t in toks if t]
    if not toks:
        return None
    picked: list[str] = []
    for i in range(len(toks) - 1, -1, -1):
        tok = toks[i]
        low = tok.lower()
        if tok.endswith(":") or low.endswith(("'s", "’s")) or low.endswith(MODIFIER_END):
            break
        if low in STOP:
            prev = toks[i - 1] if i > 0 else ""
            tail_ok = (not picked and low in NAME_SUFFIX_OK and _namelike(tok) and _namelike(prev)
                       and prev.lower() not in STOP and not prev.lower().endswith(MODIFIER_END))
            if not tail_ok:
                break
        elif not _namelike(tok):
            break
        picked.append(tok)
        if len(picked) == 4:
            break
    if picked:
        return " ".join(reversed(picked))
    if len(toks) == 1 and toks[0].lower() not in STOP:   # all-lowercase brand: "dust raises"
        return toks[0]
    return None


def extract_company(title: str) -> str | None:
    """'London-based AI startup Foo Labs raises €3M seed' -> 'Foo Labs'."""
    m = VERB.search(title or "")
    if not m:
        return None
    head = re.sub(r"\(.*?\)", "", title[:m.start()]).strip()
    # "Foo, the AI startup for lawyers," -> try clauses right-to-left
    for seg in reversed([s for s in head.split(",") if s.strip()]):
        name = _name_from_segment(seg)
        if name:
            return name
    return None


def parse_feed(xml_bytes: bytes) -> list[dict]:
    """Parse RSS 2.0 or Atom into [{title, link, date, summary}]."""
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return []
    out = []
    for el in root.iter():
        if el.tag.split("}")[-1] not in ("item", "entry"):
            continue
        f = {"title": "", "link": "", "date": "", "summary": ""}
        for ch in el:
            tag = ch.tag.split("}")[-1]
            text = (ch.text or "").strip()
            if tag == "title":
                f["title"] = strip_html(text)
            elif tag == "link" and not f["link"]:
                f["link"] = ch.attrib.get("href") or text
            elif tag in ("pubDate", "published", "updated", "date") and not f["date"]:
                try:
                    f["date"] = parsedate_to_datetime(text).date().isoformat()
                except (TypeError, ValueError, IndexError):
                    f["date"] = to_date(text)
            elif tag in ("description", "summary") and not f["summary"]:
                f["summary"] = strip_html(text)[:600]
        if f["title"]:
            out.append(f)
    return out


def seed_announcements(items: list[dict], source: str, google_news: bool, max_age_days: int,
                       today: dt.date) -> list[dict]:
    out = []
    for it in items:
        title = it["title"]
        if google_news:                     # "Headline - Publisher"
            title = re.sub(r"\s+[-–|]\s+[^-–|]{2,40}$", "", title)
        # Google News summaries are just lists of other headlines -> title only.
        text = title if google_news else title + " " + it["summary"]
        if not SEED.search(text) or LATER_ROUND.search(title):
            continue
        if it["date"]:
            try:
                if (today - dt.date.fromisoformat(it["date"])).days > max_age_days:
                    continue
            except ValueError:
                pass
        name = extract_company(title)
        if not name:
            continue
        amt = AMOUNT.search(title) or AMOUNT.search(text)
        out.append({"company": name, "announced": it["date"] or today.isoformat(),
                    "headline": title, "link": it["link"], "source": source,
                    "amount": amt.group(0).replace(" ", "") if amt else ""})
    return out


# ----------------------------------------------------------------------------
# 3. Job boards
# ----------------------------------------------------------------------------

CORP_SUFFIX = set("ai labs lab technologies technology tech hq inc ltd bv gmbh io app systems".split())


def slug_candidates(name: str) -> list[tuple[str, str]]:
    """Guess job-board slugs. Returns [(slug, 'exact'|'partial')], best first."""
    base = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    words = re.findall(r"[a-z0-9]+", base)
    if not words:
        return []
    cands = [("".join(words), "exact"), ("-".join(words), "exact")]
    core = list(words)
    while len(core) > 1 and core[-1] in CORP_SUFFIX:
        core.pop()
    if core != words:
        cands += [("".join(core), "partial"), ("-".join(core), "partial")]
    seen, out = set(), []
    for slug, kind in cands:
        if len(slug) >= 3 and slug not in seen:
            seen.add(slug)
            out.append((slug, kind))
    return out


def _job(title, location, url, posted, desc=""):
    return {"title": (title or "").strip(), "location": (location or "").strip(),
            "url": url or "", "posted": to_date(posted), "desc": strip_html(desc)[:20000]}


def norm_greenhouse(data) -> list[dict] | None:
    if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
        return None
    return [_job(j.get("title"), (j.get("location") or {}).get("name"), j.get("absolute_url"),
                 j.get("first_published") or j.get("updated_at"), j.get("content"))
            for j in data["jobs"] if isinstance(j, dict)]


def norm_lever(data) -> list[dict] | None:
    if not isinstance(data, list):
        return None
    out = []
    for j in data:
        if not isinstance(j, dict):
            continue
        cat = j.get("categories") or {}
        loc = cat.get("location") or ""
        if j.get("workplaceType") == "remote" and "remote" not in loc.lower():
            loc = (loc + " (Remote)").strip()
        out.append(_job(j.get("text"), loc, j.get("hostedUrl"), j.get("createdAt"),
                        j.get("descriptionPlain") or j.get("description")))
    return out


def norm_ashby(data) -> list[dict] | None:
    if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
        return None
    out = []
    for j in data["jobs"]:
        if not isinstance(j, dict) or j.get("isListed") is False:
            continue
        locs = [j.get("location") or ""]
        locs += [s.get("location", "") for s in (j.get("secondaryLocations") or []) if isinstance(s, dict)]
        loc = "; ".join(x for x in locs if x)
        if j.get("isRemote") and "remote" not in loc.lower():
            loc = (loc + " (Remote)").strip()
        out.append(_job(j.get("title"), loc, j.get("jobUrl") or j.get("applyUrl"),
                        j.get("publishedAt"), j.get("descriptionPlain") or j.get("descriptionHtml")))
    return out


def norm_workable(data) -> list[dict] | None:
    if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
        return None
    out = []
    for j in data["jobs"]:
        if not isinstance(j, dict):
            continue
        loc = ", ".join(x for x in (j.get("city"), j.get("country")) if x)
        if j.get("telecommuting") and "remote" not in loc.lower():
            loc = (loc + " (Remote)").strip()
        out.append(_job(j.get("title"), loc, j.get("url") or j.get("shortlink") or j.get("application_url"),
                        j.get("published_on") or j.get("created_at"), j.get("description")))
    return out


def norm_recruitee(data) -> list[dict] | None:
    if not isinstance(data, dict) or not isinstance(data.get("offers"), list):
        return None
    out = []
    for j in data["offers"]:
        if not isinstance(j, dict):
            continue
        loc = j.get("location") or ", ".join(x for x in (j.get("city"), j.get("country")) if x)
        if j.get("remote") and "remote" not in loc.lower():
            loc = (loc + " (Remote)").strip()
        out.append(_job(j.get("title"), loc, j.get("careers_url") or j.get("careers_apply_url"),
                        j.get("published_at") or j.get("created_at"), j.get("description")))
    return out


# name -> (API url template, human board url template, normaliser)
ATS = {
    "ashby": ("https://api.ashbyhq.com/posting-api/job-board/{s}",
              "https://jobs.ashbyhq.com/{s}", norm_ashby),
    "greenhouse": ("https://boards-api.greenhouse.io/v1/boards/{s}/jobs?content=true",
                   "https://boards.greenhouse.io/{s}", norm_greenhouse),
    "lever": ("https://api.lever.co/v0/postings/{s}?mode=json",
              "https://jobs.lever.co/{s}", norm_lever),
    "lever_eu": ("https://api.eu.lever.co/v0/postings/{s}?mode=json",
                 "https://jobs.eu.lever.co/{s}", norm_lever),
    "workable": ("https://apply.workable.com/api/v1/widget/accounts/{s}",
                 "https://apply.workable.com/{s}/", norm_workable),
    "recruitee": ("https://{s}.recruitee.com/api/offers/",
                  "https://{s}.recruitee.com/", norm_recruitee),
}


def fetch_board(ats: str, slug: str) -> list[dict] | None:
    api, _, norm = ATS[ats]
    return norm(get_json(api.format(s=urllib.parse.quote(slug, safe="-"))))


def find_boards(company: str) -> list[dict]:
    """Return [{ats, slug, board_match, board_url, jobs}] for every ATS with a non-empty board."""
    override = SLUG_OVERRIDES.get(company.lower())
    if override and override[0] in ATS:
        plan = {override[0]: [(override[1], "exact")]}
    else:
        cands = slug_candidates(company)
        plan = {ats: cands for ats in ATS}
    hits = []
    for ats, cands in plan.items():
        for slug, kind in cands:
            jobs = fetch_board(ats, slug)
            if jobs:                       # non-empty board found; stop guessing for this ATS
                hits.append({"ats": ats, "slug": slug, "board_match": kind,
                             "board_url": ATS[ats][1].format(s=slug), "jobs": jobs})
                break
    return hits


def rows_from_board(company: dict, board: dict, max_open_roles: int, any_level: bool = False) -> list[dict]:
    jobs = board["jobs"]
    if len(jobs) > max_open_roles:        # not "just started hiring" (or a slug collision)
        return []
    rows = []
    for j in jobs:
        match = role_match(j["title"])
        if not match or not j["url"]:
            continue
        level, years = level_of(j["title"]), min_years_required(j["desc"])
        keep, signal = is_grad_role(j["title"], j["desc"])
        if not keep and not any_level:
            continue
        rows.append({
            "source": "funding-news+" + board["ats"], "company": company["company"],
            "title": j["title"], "match": match, "level": level,
            "grad_signal": "yes" if keep else "",
            "min_years_exp": "" if years is None else years,
            "location": j["location"], "loc_tier": location_tier(j["location"]),
            "visa_or_relocation_mentioned": "yes" if VISA.search(j["desc"]) else "",
            "posted": j["posted"], "open_roles_on_board": len(jobs),
            "funding_amount": company.get("amount", ""), "funding_date": company.get("announced", ""),
            "funding_headline": company.get("headline", ""), "funding_link": company.get("link", ""),
            "board_match": board["board_match"], "url": j["url"],
        })
    return rows


# ----------------------------------------------------------------------------
# 5a. Hacker News "Who is hiring?"
# ----------------------------------------------------------------------------

HN_API = "https://hn.algolia.com/api/v1/"
HN_FUNDING = re.compile(r"\b(pre[- ]?seed|seed[- ]stage|seed round|seed funding|seed|"
                        r"just raised|recently raised|newly funded)\b", re.I)


def hn_row(hit: dict, thread_date: str, any_level: bool = False) -> dict | None:
    raw = hit.get("comment_text") or ""
    text = strip_html(re.sub(r"<p>", "\n", raw, flags=re.I))
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
    if not lines:
        return None
    fund = HN_FUNDING.search(text)
    if not fund or re.search(r"\bseries\s+[b-z]\b", text, re.I):
        return None
    core, adj = CORE_ROLE.search(text), ADJACENT_ROLE.search(text)
    if not (core or adj):
        return None
    segs = [s.strip() for s in lines[0].split("|")]
    title_seg = next((s for s in segs[1:] if role_match(s)), "")
    level = level_of(title_seg or lines[0])
    keep, _ = is_grad_role(title_seg or lines[0], text)
    if not keep and not any_level:
        return None
    s, e = max(0, fund.start() - 60), min(len(text), fund.end() + 60)
    return {
        "source": "hn-who-is-hiring", "company": segs[0][:60],
        "title": title_seg or (core or adj).group(0), "match": "core" if core else "adjacent",
        "level": level, "grad_signal": "yes" if keep else "",
        "location": " | ".join(segs[1:])[:160], "loc_tier": location_tier(lines[0]),
        "visa_or_relocation_mentioned": "yes" if VISA.search(text) else "",
        "posted": to_date(hit.get("created_at")) or thread_date, "open_roles_on_board": "",
        "funding_amount": (AMOUNT.search(text[s:e]) or [""])[0] if AMOUNT.search(text[s:e]) else "",
        "funding_date": "", "funding_headline": "…" + " ".join(text[s:e].split()) + "…",
        "funding_link": "", "board_match": "",
        "url": "https://news.ycombinator.com/item?id=%s" % hit.get("objectID", ""),
    }


def hn_rows(n_threads: int = 2, any_level: bool = False) -> list[dict]:
    data = get_json(HN_API + "search_by_date?tags=story,author_whoishiring&hitsPerPage=12")
    if not data:
        print("  ! Hacker News API unreachable", file=sys.stderr)
        return []
    threads = [h for h in data.get("hits", []) if "who is hiring" in (h.get("title") or "").lower()]
    rows, seen = [], set()
    for th in threads[:n_threads]:
        sid = th.get("objectID")
        for q in ("seed", "pre-seed", "just raised"):
            res = get_json(HN_API + "search?" + urllib.parse.urlencode(
                {"query": q, "tags": "comment,story_%s" % sid, "hitsPerPage": 500})) or {}
            for hit in res.get("hits", []):
                oid = hit.get("objectID")
                if oid in seen or str(hit.get("parent_id")) != str(sid):   # top-level posts only
                    continue
                seen.add(oid)
                row = hn_row(hit, to_date(th.get("created_at")), any_level)
                if row:
                    rows.append(row)
    return rows


# ----------------------------------------------------------------------------
# 5b. YC companies (company-level leads)
# ----------------------------------------------------------------------------

YC_AI = re.compile(r"\b(ai|a\.i\.|artificial intelligence|machine learning|ml|llms?|"
                   r"generative ai|ai agents?|deep learning|computer vision|nlp)\b", re.I)


def yc_batch_year(batch: str) -> int | None:
    m = re.search(r"(?:winter|spring|summer|fall|autumn)\s+(\d{4})", batch or "", re.I)
    if m:
        return int(m.group(1))
    m = re.fullmatch(r"[WSFX](\d{2})", (batch or "").strip(), re.I)
    return 2000 + int(m.group(1)) if m else None


def yc_row(c: dict, min_year: int, max_team: int) -> dict | None:
    year = yc_batch_year(c.get("batch", ""))
    team = c.get("team_size")
    if year is None or year < min_year or c.get("isHiring") is False:
        return None
    if isinstance(team, (int, float)) and team > max_team:
        return None
    blob = " ".join([c.get("one_liner") or "", " ".join(c.get("tags") or []),
                     " ".join(c.get("industries") or [])])
    if not YC_AI.search(blob) or not c.get("slug"):
        return None
    loc = c.get("all_locations") or ", ".join(c.get("regions") or [])
    return {
        "source": "yc-directory", "company": c.get("name", ""),
        "title": "(company-level lead - check jobs page) " + (c.get("one_liner") or "")[:90],
        "match": "company", "level": "unspecified", "location": loc, "loc_tier": location_tier(loc),
        "visa_or_relocation_mentioned": "", "posted": "", "open_roles_on_board": "",
        "funding_amount": "", "funding_date": "",
        "funding_headline": "YC %s, team size %s" % (c.get("batch", "?"), team if team is not None else "?"),
        "funding_link": c.get("website") or "", "board_match": "",
        "url": "https://www.ycombinator.com/companies/%s/jobs" % c["slug"],
    }


def yc_rows(today: dt.date, max_team: int = 15) -> list[dict]:
    data = get_json("https://yc-oss.github.io/api/companies/hiring.json")
    if not isinstance(data, list):
        print("  ! YC directory API unreachable", file=sys.stderr)
        return []
    rows = [yc_row(c, today.year - 1, max_team) for c in data if isinstance(c, dict)]
    return [r for r in rows if r]


# ----------------------------------------------------------------------------
# State + output
# ----------------------------------------------------------------------------

FIELDS = ["is_new", "first_seen", "source", "company", "title", "match", "level", "grad_signal", "min_years_exp", "location",
          "loc_tier", "visa_or_relocation_mentioned", "posted", "open_roles_on_board",
          "funding_amount", "funding_date", "funding_headline", "funding_link", "board_match", "url"]


def load_state(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            st = json.load(f)
        if isinstance(st, dict):
            st.setdefault("companies", {})
            st.setdefault("seen", {})
            st.setdefault("notified", [])
            return st
    except (OSError, ValueError):
        pass
    return {"companies": {}, "seen": {}, "notified": []}


def sort_key(r: dict):
    return (r["is_new"] != "yes",
            {"core": 0, "adjacent": 1, "company": 2}.get(r["match"], 3),
            r.get("grad_signal") != "yes",
            r["loc_tier"],
            {"junior": 0, "unspecified": 1, "senior": 2}[r["level"]],
            "".join(chr(255 - ord(c)) if ord(c) < 256 else c for c in (r["funding_date"] or r["posted"] or "")))


def http_post_json(url: str, payload: dict) -> bool:
    try:
        req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), method="POST",
                                     headers={"Content-Type": "application/json", "User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status < 300
    except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError, ValueError):
        return False


def discord_messages(rows: list[dict], limit: int = 30) -> list[str]:
    """Format rows as Discord messages, each under the 2000-character limit."""
    blocks = []
    for r in rows[:limit]:
        funding = ("seed " + str(r.get("funding_amount", ""))).strip() if r["source"].startswith("funding") else r["source"]
        meta = " · ".join(x for x in (r["location"] or "location n/a", funding,
                                      "grad-friendly" if r.get("grad_signal") else "") if x)
        blocks.append(("**%s** — %s\n%s\n<%s>" % (r["company"][:80], r["title"][:120], meta[:200], r["url"]))[:1900])
    if len(rows) > limit:
        blocks.append("…and %d more on the dashboard." % (len(rows) - limit))
    msgs, cur = [], ""
    for b in blocks:
        if cur and len(cur) + len(b) + 2 > 1900:
            msgs.append(cur)
            cur = b
        else:
            cur = cur + "\n\n" + b if cur else b
    if cur:
        msgs.append(cur)
    return msgs


def notify_discord(webhook: str, rows: list[dict]) -> bool:
    if not webhook.startswith("https://"):
        return False
    for msg in discord_messages(rows):
        # allowed_mentions: scraped text must never be able to ping @everyone
        if not http_post_json(webhook, {"content": msg, "username": "Seed Scout",
                                        "allowed_mentions": {"parse": []}}):
            return False
        time.sleep(1)
    return True


def _write(path: str):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    return open(path, "w", newline="", encoding="utf-8")


def run(args) -> int:
    today = dt.date.today()
    state = load_state(args.state)

    # 1-2. funding news -> watchlist
    if not args.no_news:
        print("Reading funding news...")
        for name, url, is_gn in FEEDS:
            body = http_get(url)
            if not body:
                print("  ! feed unavailable: %s" % name, file=sys.stderr)
                continue
            anns = seed_announcements(parse_feed(body), name, is_gn, args.days, today)
            print("  %-45.45s %2d seed announcements" % (name, len(anns)))
            for a in anns:
                state["companies"].setdefault(a["company"].lower(), a)   # keep earliest sighting
    cutoff = (today - dt.timedelta(days=args.watch_days)).isoformat()
    state["companies"] = {k: v for k, v in state["companies"].items() if v.get("announced", "") >= cutoff}
    watch = list(state["companies"].values())

    # 3-4. probe job boards
    rows: list[dict] = []
    if watch:
        print("Checking job boards for %d watchlisted companies..." % len(watch))
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            for comp, boards in zip(watch, pool.map(lambda c: find_boards(c["company"]), watch)):
                for b in boards:
                    rows += rows_from_board(comp, b, args.max_open_roles, args.any_level)

    # 5. extra sources
    if not args.no_hn:
        print("Reading Hacker News 'Who is hiring?'...")
        rows += hn_rows(args.hn_threads, args.any_level)
    if args.include_yc:      # off by default: company-level leads, not verified roles
        print("Reading YC directory...")
        rows += yc_rows(today)

    # filters
    if args.core_only:
        rows = [r for r in rows if r["match"] == "core"]
    if not args.any_location:
        rows = [r for r in rows if not r["location"] or r["loc_tier"] < len(LOCATION_TIERS)]

    # dedupe + new/seen
    uniq = {}
    for r in rows:
        uniq.setdefault(r["url"], r)
    rows = list(uniq.values())
    for r in rows:
        r.setdefault("grad_signal", "")
        r.setdefault("min_years_exp", "")
        first = state["seen"].setdefault(r["url"], today.isoformat())
        r["first_seen"] = first
        r["is_new"] = "yes" if first == today.isoformat() else ""
    rows.sort(key=sort_key)

    with _write(args.out) as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    if args.json:
        with _write(args.json) as f:
            json.dump({"generated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                       "rows": [{k: r.get(k, "") for k in FIELDS} for r in rows]}, f, ensure_ascii=False)

    # Discord: each role is announced once, ever (tracked in the state file)
    notified = set(state.get("notified", []))
    to_send = [r for r in rows if r["url"] not in notified]
    webhook = args.discord_webhook or os.environ.get("DISCORD_WEBHOOK_URL", "")
    if webhook and to_send:
        if notify_discord(webhook, to_send):
            print("Posted %d new roles to Discord" % len(to_send))
            notified.update(r["url"] for r in to_send)
        else:
            print("  ! Discord post failed - will retry next run", file=sys.stderr)
    elif not webhook:
        notified.update(r["url"] for r in to_send)
    state["notified"] = sorted(notified)
    with _write(args.state) as f:
        json.dump(state, f, indent=1, ensure_ascii=False)

    new = [r for r in rows if r["is_new"]]
    print("\n%d matching roles/leads (%d new today) -> %s" % (len(rows), len(new), args.out))
    for r in new[:25]:
        print("  [%s] %s - %s (%s) %s" % (r["match"], r["company"], r["title"][:60],
                                         r["location"][:40] or "location n/a", r["url"]))
    if len(new) > 25:
        print("  ... and %d more in the CSV" % (len(new) - 25))
    return 0


# ----------------------------------------------------------------------------
# Offline self-test
# ----------------------------------------------------------------------------


def selftest() -> int:
    eq_fail = []

    def eq(got, want, label):
        if got != want:
            eq_fail.append("%s: got %r, want %r" % (label, got, want))

    for title, want in [
        ("London-based AI startup Foo Labs raises €3M seed round", "Foo Labs"),
        ("Dutch healthtech Medvia secures €2.5 million in seed funding", "Medvia"),
        ("AI Startup Qorvex Raises $4M Seed Round To Automate Audits", "Qorvex"),
        ("Foo AI raises $5M seed to build agents", "Foo AI"),
        ("Dutch AI startup Foo AI raises $5M", "Foo AI"),
        ("Brightwave, the AI startup for lawyers, raises $6M seed", "Brightwave"),
        ("Amsterdam, Netherlands-based Kiln lands pre-seed", "Kiln"),
        ("Exclusive: Nimbus bags £2M seed", "Nimbus"),
        ("Amsterdam’s Tolq closes €1.8M pre-seed", "Tolq"),
        ("YC-backed Reflow emerges from stealth with $3M", "Reflow"),
        ("dust raises $5M seed", "dust"),
        ("pony.ai raises seed", "pony.ai"),
        ("How Zed raised its seed round", "Zed"),
        ("London-based legal AI raises seed", None),
        ("Seed funding is down 20% in Europe", None),
        ("", None),
    ]:
        eq(extract_company(title), want, "extract_company(%r)" % title)

    eq(slug_candidates("Foo Labs"), [("foolabs", "exact"), ("foo-labs", "exact"), ("foo", "partial")], "slug1")
    eq(slug_candidates("Médvia"), [("medvia", "exact")], "slug2")
    eq(slug_candidates("pony.ai"), [("ponyai", "exact"), ("pony-ai", "exact"), ("pony", "partial")], "slug3")
    eq(slug_candidates("AI"), [], "slug too short")
    eq(slug_candidates("!!!"), [], "slug empty")

    for t, want in [("Applied AI Engineer", "core"), ("Senior Machine Learning Engineer", "core"),
                    ("ML Engineer (LLMs)", "core"), ("Founding Engineer", "adjacent"),
                    ("Account Executive", ""), ("Email Marketing Manager", ""), ("", "")]:
        eq(role_match(t), want, "role_match(%r)" % t)
    eq(level_of("Senior ML Engineer"), "senior", "level senior")
    eq(level_of("Graduate Applied AI Engineer"), "junior", "level junior")
    eq(level_of("ML Engineer"), "unspecified", "level none")

    eq(location_tier("Amsterdam, NL"), 0, "loc nl")
    eq(location_tier("London, UK"), 1, "loc uk")
    eq(location_tier("Kyiv, Ukraine"), 2, "loc ukraine is not uk")
    eq(location_tier("Remote (Europe)"), 2, "loc remote europe excluded")
    eq(location_tier("Remote (UK)"), 1, "loc remote uk")
    eq(location_tier("Cambridge, MA"), 2, "loc cambridge ma")
    eq(location_tier("Cambridge, UK"), 1, "loc cambridge uk")
    eq(location_tier(""), 2, "loc empty")

    eq(to_date(1767225600000), "2026-01-01", "date ms")
    eq(to_date("2026-10-02T09:00:00.000+00:00"), "2026-10-02", "date iso")
    eq(to_date("2026-10-02 10:00:00 UTC"), "2026-10-02", "date recruitee")
    eq(to_date(None), "", "date none")
    eq(to_date("soon"), "", "date junk")

    rss = b"""<?xml version="1.0"?><rss version="2.0"><channel>
      <item><title>Amsterdam-based Kiln raises &#8364;3M seed round for AI agents</title>
        <link>https://example.com/kiln</link><pubDate>Tue, 10 Nov 2026 09:00:00 +0000</pubDate>
        <description><![CDATA[<p>Kiln has raised funding.</p>]]></description></item>
      <item><title>Voltic secures $40M Series B</title><link>https://example.com/v</link>
        <pubDate>Tue, 10 Nov 2026 09:00:00 +0000</pubDate><description>Earlier seed investors joined.</description></item>
      <item><title>Oldco raises seed</title><link>https://example.com/o</link>
        <pubDate>Mon, 01 Jan 2024 09:00:00 +0000</pubDate></item>
      <item><title>Harbor raises $2M</title><link>https://example.com/h</link>
        <pubDate>Mon, 09 Nov 2026 09:00:00 +0000</pubDate><description>The pre-seed round was led by X.</description></item>
    </channel></rss>"""
    anns = seed_announcements(parse_feed(rss), "t", False, 60, dt.date(2026, 11, 14))
    eq([a["company"] for a in anns], ["Kiln", "Harbor"], "rss pipeline")
    eq(anns[0]["amount"], "€3M", "rss amount")
    eq(anns[0]["announced"], "2026-11-10", "rss date")
    atom = b"""<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Nimbus bags seed</title>
      <link href="https://example.com/n"/><updated>2026-11-12T10:00:00Z</updated></entry></feed>"""
    a = seed_announcements(parse_feed(atom), "t", False, 60, dt.date(2026, 11, 14))
    eq([(x["company"], x["link"], x["announced"]) for x in a],
       [("Nimbus", "https://example.com/n", "2026-11-12")], "atom")
    gn = [{"title": "Kiln raises $3M seed round - TechCrunch", "link": "l", "date": "", "summary": "x"}]
    eq(seed_announcements(gn, "g", True, 60, dt.date(2026, 11, 14))[0]["company"], "Kiln", "google news")
    eq(parse_feed(b"not xml"), [], "bad xml")
    eq(parse_feed(b"<rss><channel></channel></rss>"), [], "empty feed")

    gh = norm_greenhouse({"jobs": [{"title": "Applied AI Engineer", "location": {"name": "Amsterdam"},
                                    "absolute_url": "u1", "updated_at": "2026-11-01T00:00:00-04:00",
                                    "content": "&lt;p&gt;We offer visa sponsorship&lt;/p&gt;"}]})
    eq((gh[0]["location"], gh[0]["posted"], gh[0]["desc"]), ("Amsterdam", "2026-11-01", "We offer visa sponsorship"), "gh")
    lv = norm_lever([{"text": "ML Engineer", "categories": {"location": "London"}, "hostedUrl": "u2",
                      "createdAt": 1767225600000, "workplaceType": "remote"}])
    eq((lv[0]["location"], lv[0]["posted"]), ("London (Remote)", "2026-01-01"), "lever")
    eq(norm_lever({"ok": False, "error": "Document not found"}), None, "lever 404 body")
    ab = norm_ashby({"jobs": [{"title": "Founding Engineer", "location": "Amsterdam", "isRemote": False,
                               "secondaryLocations": [{"location": "London"}], "jobUrl": "u3",
                               "publishedAt": "2026-11-05T12:00:00.000+00:00", "isListed": True},
                              {"title": "Hidden", "isListed": False}]})
    eq((len(ab), ab[0]["location"]), (1, "Amsterdam; London"), "ashby")
    wk = norm_workable({"name": "X", "jobs": [{"title": "NLP Engineer", "city": "Utrecht",
                                              "country": "Netherlands", "url": "u4", "published_on": "2026-11-03"}]})
    eq(wk[0]["location"], "Utrecht, Netherlands", "workable")
    rc = norm_recruitee({"offers": [{"title": "Machine Learning Engineer", "location": "Rotterdam",
                                    "careers_url": "u5", "published_at": "2026-11-02 08:00:00 UTC"}]})
    eq(rc[0]["posted"], "2026-11-02", "recruitee")
    for fn in (norm_greenhouse, norm_ashby, norm_workable, norm_recruitee, norm_lever):
        eq(fn(None), None, fn.__name__ + "(None)")
        eq(fn({"jobs": [None, 3]} if fn is not norm_lever else [None, 3]) in ([], None), True, fn.__name__ + " junk")

    comp = {"company": "Kiln", "amount": "€3M", "announced": "2026-11-10", "headline": "h", "link": "l"}
    board = {"ats": "ashby", "slug": "kiln", "board_match": "exact", "board_url": "b", "jobs": gh + lv + ab + [
        _job("Account Executive", "London", "u9", None)]}
    rows = rows_from_board(comp, board, 20, any_level=True)
    eq([(r["title"], r["match"]) for r in rows],
       [("Applied AI Engineer", "core"), ("ML Engineer", "core"), ("Founding Engineer", "adjacent")], "board rows")
    eq(rows[0]["visa_or_relocation_mentioned"], "yes", "visa flag")
    eq(rows_from_board(comp, board, 3, any_level=True), [], "too many open roles")
    eq(rows_from_board(comp, board, 20), [], "no grad signal -> dropped")

    eq(level_of("ML Engineer II"), "senior", "level II")
    eq(min_years_required("We need 3-5 years of experience in ML"), 3, "years range")
    eq(min_years_required("5+ years' professional experience; 2 years of experience with PyTorch"), 5, "years max")
    eq(min_years_required("Founded 10 years ago. Great team."), None, "years unrelated")
    eq(min_years_required(""), None, "years empty")
    b2 = dict(board, jobs=[_job("Senior ML Engineer", "London", "s1", None),
                           _job("ML Engineer", "London", "s2", None, "Requires 4+ years of experience"),
                           _job("Graduate ML Engineer", "London", "s3", None),
                           _job("ML Engineer II", "London", "s4", None),
                           _job("AI Engineer", "London", "s5", None, "Recent graduates welcome. 1+ years of experience ideal")])
    r2 = rows_from_board(comp, b2, 20)
    eq([(r["url"], r["grad_signal"], r["min_years_exp"]) for r in r2], [("s3", "yes", ""), ("s5", "yes", 1)], "level filter")
    eq(len(rows_from_board(comp, b2, 20, any_level=True)), 5, "any level")
    for t, d, want in [("Lead ML Engineer", "new grads welcome", False),
                       ("Tech Lead, Applied AI", "", False),
                       ("Machine Learning Intern", "graduates", False),
                       ("ML Engineer", "You will mentor junior engineers.", False),
                       ("ML Engineer", "Great for recent graduates.", True),
                       ("Junior ML Engineer", "", True),
                       ("Graduate AI Engineer", "3+ years of experience required", False),
                       ("Associate Machine Learning Engineer", "", True)]:
        eq(is_grad_role(t, d)[0], want, "is_grad_role(%r, %r)" % (t, d))
    msgs = discord_messages(r2)
    eq((len(msgs), "<s3>" in msgs[0], "@" in msgs[0]), (1, True, False), "discord one msg")
    many = [dict(r2[0], url="https://example.com/" + "x" * 150 + str(i)) for i in range(40)]
    msgs = discord_messages(many)
    eq((all(len(m) <= 1900 for m in msgs), "10 more" in msgs[-1], len(msgs) > 1), (True, True, True), "discord chunking")
    eq(discord_messages([]), [], "discord empty")
    eq(hn_row({"comment_text": "Kiln | Senior ML Engineer | London<p>seed round"}, ""), None, "hn senior")
    eq(hn_row({"comment_text": "Kiln | ML Engineer | London<p>seed round"}, ""), None, "hn no grad signal")

    hn = {"objectID": "42", "parent_id": 7, "created_at": "2026-11-02T15:00:00Z",
          "comment_text": "Kiln | Applied AI Engineer | Amsterdam | ONSITE | Visa<p>We just closed a $3M seed round "
                          "and are hiring our first ML engineers. New grads welcome."}
    r = hn_row(hn, "2026-11-01")
    eq((r["company"], r["title"], r["match"], r["loc_tier"], r["url"]),
       ("Kiln", "Applied AI Engineer", "core", 0, "https://news.ycombinator.com/item?id=42"), "hn row")
    eq(hn_row({"comment_text": "BigCo | ML Engineer<p>seed in 2019, now Series C"}, ""), None, "hn later round")
    eq(hn_row({"comment_text": "Shop | Sales rep<p>seed round"}, ""), None, "hn wrong role")
    eq(hn_row({"comment_text": ""}, ""), None, "hn empty")

    yc = {"name": "Reflow", "slug": "reflow", "batch": "Summer 2026", "team_size": 4, "isHiring": True,
          "one_liner": "AI agents for plumbers", "tags": ["B2B"], "industries": ["B2B"], "all_locations": "London"}
    eq(yc_row(yc, 2025, 15)["url"], "https://www.ycombinator.com/companies/reflow/jobs", "yc ok")
    eq(yc_row(dict(yc, batch="Winter 2019"), 2025, 15), None, "yc old batch")
    eq(yc_row(dict(yc, team_size=200), 2025, 15), None, "yc big team")
    eq(yc_row(dict(yc, one_liner="Payroll for dentists"), 2025, 15), None, "yc not ai")
    eq(yc_batch_year("W25"), 2025, "yc short batch")
    eq(yc_batch_year(""), None, "yc no batch")

    if eq_fail:
        print("SELFTEST FAILED:\n  " + "\n  ".join(eq_fail))
        return 1
    print("selftest: all checks passed")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--out", default="seed_jobs.csv")
    p.add_argument("--state", default="seed_scout_state.json")
    p.add_argument("--days", type=int, default=60, help="ignore funding news older than this (default 60)")
    p.add_argument("--watch-days", type=int, default=180,
                   help="keep re-checking a company's job board this long after its round (default 180)")
    p.add_argument("--max-open-roles", type=int, default=20,
                   help="skip boards with more openings than this (default 20)")
    p.add_argument("--core-only", action="store_true", help="only Applied AI / ML titles, no adjacent titles")
    p.add_argument("--any-location", action="store_true", help="don't restrict to the Netherlands / UK")
    p.add_argument("--any-level", action="store_true", help="keep senior / mid-level roles too")
    p.add_argument("--json", default="", help="also write results as JSON (for the dashboard), e.g. docs/jobs.json")
    p.add_argument("--discord-webhook", default="", help="Discord webhook URL (or set DISCORD_WEBHOOK_URL)")
    p.add_argument("--no-news", action="store_true")
    p.add_argument("--no-hn", action="store_true")
    p.add_argument("--include-yc", action="store_true", help="add YC company-level leads (off by default)")
    p.add_argument("--hn-threads", type=int, default=2, help="how many monthly HN threads to read (default 2)")
    p.add_argument("--workers", type=int, default=6)
    p.add_argument("--selftest", action="store_true")
    args = p.parse_args()
    return selftest() if args.selftest else run(args)


if __name__ == "__main__":
    sys.exit(main())
