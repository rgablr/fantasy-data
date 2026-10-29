#!/usr/bin/env python3
"""Daily FantasyCalc value history (Dynasty Tool, player Value tab chart).

FantasyCalc only keeps each player's value history from January 1 of the current year, so this keeps
our own copy that grows season after season. Once a day it saves today's value of every player, in four
formats (12 teams, full PPR):

    dyn_sf   dynasty superflex      dyn_1qb  dynasty 1QB
    red_sf   redraft superflex      red_1qb  redraft 1QB

Files (FantasyCalc's raw numbers; the app shows them divided by 1000):
    data/fc/<format>/<YYYY-MM>.json   {"format", "month", "p": {sleeper_id: [value for day 1, day 2, ...]}}
                                      (null where there is no value that day)
    data/fc/index.json                {"formats": {format: ["YYYY-MM", ...]}, "updated": "YYYY-MM-DD"}

One file per month, so a day's run only rewrites that month's file and the repo stays small.

Backfill: FantasyCalc's own history (trades/implied/<FantasyCalc id>) fills every day this year for a
player the first time he is seen (the first run backfills everyone; later runs only new players, such as
rookies). Its date labels are sometimes scrambled, so each value is dated by its place in the list (one
value per day, in order), with the start day most of the labels agree on.

Standard library only. Exits non-zero if FantasyCalc returns too little, so a bad day shows up in Actions.
Run by hand: python3 scripts/fc_snapshot.py  (add --backfill-all to refill every player this year).
"""
import json
import os
import sys
import time
import urllib.request
from collections import Counter
from datetime import date, datetime, timedelta, timezone

API = "https://api.fantasycalc.com"
FORMATS = {"dyn_sf": ("true", 2), "dyn_1qb": ("true", 1), "red_sf": ("false", 2), "red_1qb": ("false", 1)}
OUT = os.path.join("data", "fc")
NEW_PLAYER_LIMIT = 80   # history calls for new players per format per run (the first run is unlimited)


def get(url):
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "fantasy-data-fc-history"})
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)
        except Exception:  # network hiccup: wait and try again
            if attempt == 3:
                raise
            time.sleep(2 + attempt * 3)


def qs(dyn, qbs):
    return f"isDynasty={dyn}&numQbs={qbs}&numTeams=12&ppr=1"


def current(dyn, qbs):
    """Today's values: {sleeper_id: (value, fantasycalc_id)}."""
    out = {}
    for x in get(f"{API}/values/current?{qs(dyn, qbs)}"):
        p = x.get("player") or {}
        sid, fid, v = p.get("sleeperId"), p.get("id"), x.get("value")
        if sid and fid is not None and v is not None and p.get("position") != "PICK":
            out[str(sid)] = (int(v), fid)
    return out


def history(fid, dyn, qbs):
    """FantasyCalc's daily history for one player: {date: value}, dated by place in the list."""
    h = (get(f"{API}/trades/implied/{fid}?{qs(dyn, qbs)}") or {}).get("historicalValues") or []
    starts = []
    for i, x in enumerate(h):
        try:
            m, d, y = (int(n) for n in str(x.get("date")).split("/"))
            starts.append(date(y, m, d) - timedelta(days=i))
        except (ValueError, OverflowError):   # a label that isn't a real date
            pass
    if not starts:
        return {}
    start = Counter(starts).most_common(1)[0][0]
    return {start + timedelta(days=i): int(x["value"]) for i, x in enumerate(h) if x.get("value")}


def month_path(fmt, ym):
    return os.path.join(OUT, fmt, ym + ".json")


class Months:
    """Month files for one format, loaded as needed and written back if changed."""

    def __init__(self, fmt):
        self.fmt, self.docs, self.dirty = fmt, {}, set()

    def doc(self, ym):
        if ym not in self.docs:
            path = month_path(self.fmt, ym)
            if os.path.exists(path):
                with open(path) as f:
                    self.docs[ym] = json.load(f)
            else:
                self.docs[ym] = {"format": self.fmt, "month": ym, "p": {}}
        return self.docs[ym]

    def put(self, sid, day, value):
        ym, i = day.strftime("%Y-%m"), day.day - 1
        row = self.doc(ym)["p"].setdefault(sid, [])
        while len(row) <= i:
            row.append(None)
        if row[i] != value:
            row[i] = value
            self.dirty.add(ym)

    def known(self, sid, year):
        """Has this player any saved value this year (so no backfill is needed)?"""
        for m in range(1, 13):
            ym = f"{year}-{m:02d}"
            if os.path.exists(month_path(self.fmt, ym)) or ym in self.docs:
                if any(v is not None for v in self.doc(ym)["p"].get(sid, [])):
                    return True
        return False

    def save(self):
        os.makedirs(os.path.join(OUT, self.fmt), exist_ok=True)
        for ym in sorted(self.dirty):
            with open(month_path(self.fmt, ym), "w") as f:
                json.dump(self.docs[ym], f, separators=(",", ":"))
        return sorted(self.dirty)


def main():
    today = datetime.now(timezone.utc).date()
    refill = "--backfill-all" in sys.argv
    first_run = not os.path.exists(os.path.join(OUT, "index.json"))
    notes = []
    for fmt, (dyn, qbs) in FORMATS.items():
        now = current(dyn, qbs)
        if len(now) < (150 if dyn == "true" else 80):
            sys.exit(f"{fmt}: only {len(now)} players from FantasyCalc; not saving")
        M = Months(fmt)
        new = [sid for sid in now if refill or first_run or not M.known(sid, today.year)]
        if not (refill or first_run):
            new = new[:NEW_PLAYER_LIMIT]
        filled = 0
        for sid in new:
            try:
                for day, v in history(now[sid][1], dyn, qbs).items():
                    if day.year == today.year and day <= today:
                        M.put(sid, day, v)
                filled += 1
            except Exception as e:
                print(f"{fmt} {sid}: history failed ({e})")
        for sid, (v, _) in now.items():
            M.put(sid, today, v)
        changed = M.save()
        notes.append(f"{fmt}: {len(now)} players, {filled} backfilled, months written {', '.join(changed) or 'none'}")
    # Index of every saved month per format.
    idx = {"formats": {}, "updated": today.isoformat()}
    for fmt in FORMATS:
        d = os.path.join(OUT, fmt)
        idx["formats"][fmt] = sorted(n[:-5] for n in os.listdir(d) if n.endswith(".json")) if os.path.isdir(d) else []
    with open(os.path.join(OUT, "index.json"), "w") as f:
        json.dump(idx, f, separators=(",", ":"))
    print("\n".join(notes))


if __name__ == "__main__":
    main()
