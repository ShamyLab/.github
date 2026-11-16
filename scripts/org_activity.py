#!/usr/bin/env python3
"""
Build a GitHub-style contribution calendar for a whole organization.

Walks every repo in the org (private ones included, if the token can read them),
collects commits from the last year across all branches, de-duplicates them by
SHA, and renders light + dark SVGs that look like the calendar on a user profile.

Only dates and counts end up in the output, so it's safe to show publicly even
when every repo is private.

Env vars:
  GH_TOKEN        token with read access to the org's repos (required)
  ORG             org login                        (default: ShamyLab)
  TZ_NAME         IANA time zone for bucketing days (default: UTC)
  OUT_DIR         where SVGs/JSON are written       (default: profile/assets)
  EXCLUDE_REPOS   comma list of repos to skip       (default: .github,.github-private)
  INCLUDE_FORKS   "true" to count forked repos      (default: false)
  DEMO            "true" to render random data without calling the API
"""

from __future__ import annotations

import datetime as dt
import json
import os
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

API = "https://api.github.com"
ORG = os.environ.get("ORG", "ShamyLab")
TOKEN = os.environ.get("GH_TOKEN", "")
TZ = ZoneInfo(os.environ.get("TZ_NAME", "UTC"))
OUT_DIR = os.environ.get("OUT_DIR", "profile/assets")
EXCLUDE = {r.strip().lower() for r in os.environ.get("EXCLUDE_REPOS", ".github,.github-private").split(",") if r.strip()}
INCLUDE_FORKS = os.environ.get("INCLUDE_FORKS", "false").lower() == "true"
DEMO = os.environ.get("DEMO", "false").lower() == "true"

WEEKS = 53


# --------------------------------------------------------------------------- API

def _get(url: str):
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": f"{ORG}-org-activity",
        **({"Authorization": f"Bearer {TOKEN}"} if TOKEN else {}),
    })
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.load(resp), resp.headers.get("Link", "")
        except urllib.error.HTTPError as e:
            if e.code == 409:  # empty repository
                return [], ""
            if e.code in (403, 429) and e.headers.get("Retry-After"):
                time.sleep(int(e.headers["Retry-After"]) + 1)
                continue
            if e.code >= 500 and attempt < 3:
                time.sleep(2 ** attempt)
                continue
            raise
    raise RuntimeError(f"giving up on {url}")


def _paged(path: str, params: dict | None = None):
    params = {"per_page": 100, **(params or {})}
    url = f"{API}{path}?{urllib.parse.urlencode(params)}"
    while url:
        data, link = _get(url)
        yield from data
        url = None
        for part in link.split(","):
            if 'rel="next"' in part:
                url = part[part.find("<") + 1:part.find(">")]


def _is_bot(c: dict) -> bool:
    author = c.get("author") or {}
    login = (author.get("login") or "").lower()
    name = ((c.get("commit") or {}).get("author") or {}).get("name", "").lower()
    return author.get("type") == "Bot" or login.endswith("[bot]") or name.endswith("[bot]")


def fetch_counts(since: dt.datetime) -> tuple[dict[dt.date, int], list[str]]:
    repos = [
        r for r in _paged(f"/orgs/{ORG}/repos", {"type": "all"})
        if r["name"].lower() not in EXCLUDE and (INCLUDE_FORKS or not r.get("fork"))
    ]
    if not repos:
        sys.exit(f"No repos visible in {ORG}. Does the token have access to the org's repositories?")

    since_iso = since.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    seen: set[str] = set()
    counts: dict[dt.date, int] = {}

    for repo in repos:
        full = repo["full_name"]
        branches = [b["name"] for b in _paged(f"/repos/{full}/branches")] or [repo["default_branch"]]
        n_before = len(seen)
        for branch in branches:
            for c in _paged(f"/repos/{full}/commits", {"sha": branch, "since": since_iso}):
                if c["sha"] in seen or _is_bot(c):
                    continue
                seen.add(c["sha"])
                when = dt.datetime.fromisoformat(c["commit"]["author"]["date"].replace("Z", "+00:00"))
                day = when.astimezone(TZ).date()
                counts[day] = counts.get(day, 0) + 1
        print(f"  {full}: {len(seen) - n_before} commits across {len(branches)} branch(es)")

    return counts, [r["name"] for r in repos]


def demo_counts(start: dt.date, end: dt.date) -> tuple[dict[dt.date, int], list[str]]:
    rnd = random.Random(7)
    counts, d = {}, start
    while d <= end:
        busy = 0.35 + 0.5 * ((d - start).days / (end - start).days)  # ramps up over the year
        if d.weekday() >= 5:
            busy *= 0.6
        if rnd.random() < busy:
            counts[d] = max(1, int(rnd.expovariate(1 / 5)))
        d += dt.timedelta(days=1)
    return counts, ["a", "b", "c", "d", "e", "f"]


# ------------------------------------------------------------------------- stats

def summarize(counts: dict[dt.date, int], start: dt.date, today: dt.date) -> dict:
    days = [start + dt.timedelta(days=i) for i in range((today - start).days + 1)]
    total = sum(counts.get(d, 0) for d in days)
    active = sum(1 for d in days if counts.get(d, 0) > 0)

    longest = run = 0
    for d in days:
        run = run + 1 if counts.get(d, 0) else 0
        longest = max(longest, run)

    # current streak: today counts if active, otherwise start from yesterday
    current, d = 0, today if counts.get(today, 0) else today - dt.timedelta(days=1)
    while d >= start and counts.get(d, 0):
        current += 1
        d -= dt.timedelta(days=1)

    best_day = max(days, key=lambda x: counts.get(x, 0))
    return {
        "total": total,
        "active_days": active,
        "current_streak": current,
        "longest_streak": longest,
        "best_day": best_day.isoformat() if counts.get(best_day) else None,
        "best_day_count": counts.get(best_day, 0),
    }


def levels(counts: dict[dt.date, int]):
    """Quartiles of non-zero days, like GitHub's own buckets."""
    vals = sorted(v for v in counts.values() if v > 0)
    if not vals:
        return lambda v: 0
    q = [vals[min(len(vals) - 1, int(len(vals) * p))] for p in (0.25, 0.5, 0.75)]
    return lambda v: 0 if v <= 0 else 1 + sum(v > t for t in q)


# ------------------------------------------------------------------------ render

THEMES = {
    "light": {
        "cells": ["#ebedf0", "#9be9a8", "#40c463", "#30a14e", "#216e39"],
        "stroke": "rgba(27,31,35,0.06)",
        "text": "#1f2328", "muted": "#59636e", "border": "#d1d9e0", "bg": "#ffffff",
    },
    "dark": {
        "cells": ["#151b23", "#033a16", "#196c2e", "#2ea043", "#56d364"],
        "stroke": "rgba(255,255,255,0.05)",
        "text": "#f0f6fc", "muted": "#9198a1", "border": "#3d444d", "bg": "#0d1117",
    },
}

FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI','Noto Sans',Helvetica,Arial,sans-serif"
CELL, GAP = 11, 3
STEP = CELL + GAP
PAD = 16
LEFT = 30          # room for weekday labels
TOP = 48           # header + month labels


def render(counts, start, today, stats, theme, repo_count) -> str:
    t = THEMES[theme]
    lvl = levels(counts)
    grid_w = WEEKS * STEP - GAP
    width = PAD * 2 + LEFT + grid_w
    grid_bottom = TOP + 7 * STEP - GAP
    height = grid_bottom + 96

    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" '
        f'aria-label="{stats["total"]} commits in the last year across {ORG}">',
        f'<style>text{{font-family:{FONT};fill:{t["text"]}}}.m{{fill:{t["muted"]};font-size:10px}}'
        f'.h{{font-size:14px;font-weight:400}}.v{{font-size:20px;font-weight:600}}'
        f'.l{{fill:{t["muted"]};font-size:11px}}</style>',
        f'<rect x="0.5" y="0.5" width="{width - 1}" height="{height - 1}" rx="6" '
        f'fill="{t["bg"]}" stroke="{t["border"]}"/>',
        f'<text class="h" x="{PAD}" y="{PAD + 10}">{stats["total"]:,} commits in the last year '
        f'<tspan class="l" dx="4">across {repo_count} repositories</tspan></text>',
    ]

    gx = PAD + LEFT
    # weekday labels
    for row, name in ((1, "Mon"), (3, "Wed"), (5, "Fri")):
        out.append(f'<text class="m" x="{PAD}" y="{TOP + row * STEP + CELL - 2}">{name}</text>')

    # cells + month labels
    last_month_col = -3
    for w in range(WEEKS):
        week_start = start + dt.timedelta(weeks=w)
        for r in range(7):
            d = week_start + dt.timedelta(days=r)
            if d > today:
                break
            if d.day == 1 or (w == 0 and r == 0):
                if w - last_month_col >= 3 and (w < WEEKS - 1 or r == 0):
                    out.append(f'<text class="m" x="{gx + w * STEP}" y="{TOP - 6}">{d.strftime("%b")}</text>')
                    last_month_col = w
            v = counts.get(d, 0)
            out.append(
                f'<rect x="{gx + w * STEP}" y="{TOP + r * STEP}" width="{CELL}" height="{CELL}" rx="2" '
                f'fill="{t["cells"][lvl(v)]}" stroke="{t["stroke"]}"/>'
            )

    # legend
    ly = grid_bottom + 12
    lx = gx + grid_w - (5 * STEP - GAP) - 30
    out.append(f'<text class="m" x="{lx - 6}" y="{ly + CELL - 2}" text-anchor="end">Less</text>')
    for i, c in enumerate(t["cells"]):
        out.append(f'<rect x="{lx + i * STEP}" y="{ly}" width="{CELL}" height="{CELL}" rx="2" '
                   f'fill="{c}" stroke="{t["stroke"]}"/>')
    out.append(f'<text class="m" x="{lx + 5 * STEP + 3}" y="{ly + CELL - 2}">More</text>')
    out.append(f'<text class="m" x="{gx}" y="{ly + CELL - 2}">Updated {today.strftime("%b %-d, %Y")}</text>')

    # stats strip
    sy = ly + 26
    out.append(f'<line x1="{PAD}" x2="{width - PAD}" y1="{sy}" y2="{sy}" stroke="{t["border"]}"/>')
    best = (dt.date.fromisoformat(stats["best_day"]).strftime("%b %-d") if stats["best_day"] else "—")
    tiles = [
        (f'{stats["total"]:,}', "commits"),
        (f'{stats["active_days"]}', "active days"),
        (f'{stats["current_streak"]}d', "current streak"),
        (f'{stats["longest_streak"]}d', "longest streak"),
        (f'{stats["best_day_count"]}', f"busiest day · {best}"),
    ]
    col_w = (width - 2 * PAD) / len(tiles)
    for i, (val, label) in enumerate(tiles):
        x = PAD + i * col_w + 4
        out.append(f'<text class="v" x="{x:.1f}" y="{sy + 30}">{val}</text>')
        out.append(f'<text class="l" x="{x:.1f}" y="{sy + 47}">{label}</text>')

    out.append("</svg>")
    return "\n".join(out)


# -------------------------------------------------------------------------- main

def main():
    today = dt.datetime.now(TZ).date()
    # same window as a profile: 52 full weeks back, snapped to Sunday, plus this week
    start = today - dt.timedelta(weeks=WEEKS - 1)
    start -= dt.timedelta(days=(start.weekday() + 1) % 7)

    if DEMO:
        counts, repos = demo_counts(start, today)
    else:
        if not TOKEN:
            sys.exit("GH_TOKEN is not set")
        print(f"Collecting commits for {ORG} since {start} ({TZ.key})")
        since = dt.datetime.combine(start, dt.time.min, tzinfo=TZ)
        counts, repos = fetch_counts(since)

    stats = summarize(counts, start, today)
    os.makedirs(OUT_DIR, exist_ok=True)
    for theme in THEMES:
        with open(os.path.join(OUT_DIR, f"activity-{theme}.svg"), "w") as f:
            f.write(render(counts, start, today, stats, theme, len(repos)))
    with open(os.path.join(OUT_DIR, "activity.json"), "w") as f:
        json.dump({
            "org": ORG, "generated": today.isoformat(), "time_zone": TZ.key, "repo_count": len(repos), **stats,
            "days": {d.isoformat(): v for d, v in sorted(counts.items()) if d >= start},
        }, f, indent=1)
    print(f"{stats['total']} commits, {stats['active_days']} active days -> {OUT_DIR}")


if __name__ == "__main__":
    main()
