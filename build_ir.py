#!/usr/bin/env python3
"""Build injured-reserve week lists for older seasons from nflverse weekly rosters.

Writes data/hist/<Y>.ir.json for each season Y (2002 onward):
  {"season": Y, "ir": {"<gsis_id>": [week, ...]}, "back": {"<gsis_id>": [week, ...]}}
"ir" lists the weeks a player's weekly roster status is RES (reserve/injured).
"back" lists the weeks he returned to ACT right after being RES (activated).
Players are keyed by gsis_id (the app maps a Sleeper player to it).

Source: nflverse-data release "weekly_rosters" (roster_weekly_<year>.csv).

Usage:
  python3 build_ir.py              # 2002-2025
  python3 build_ir.py 2010         # one season
  python3 build_ir.py 2010 2015    # inclusive range

Stdlib only.
"""
import csv, io, json, os, sys, urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(ROOT, "data", "hist")
BASE = "https://github.com/nflverse/nflverse-data/releases/download/weekly_rosters/roster_weekly_%d.csv"
FIRST, LAST = 2002, 2025
POS = {"QB", "RB", "WR", "TE"}

def main():
    a = [int(x) for x in sys.argv[1:]]
    y0, y1 = (a[0], a[0]) if len(a) == 1 else (a[0], a[1]) if len(a) == 2 else (FIRST, LAST)
    os.makedirs(OUT, exist_ok=True)
    for y in range(y0, y1 + 1):
        try:
            raw = urllib.request.urlopen(BASE % y, timeout=120).read().decode("utf-8")
        except Exception as e:
            print(y, "skipped:", e); continue
        ir = {}
        stat = {}
        for r in csv.DictReader(io.StringIO(raw)):
            if r.get("position") not in POS or r.get("game_type") not in ("REG", "CON", ""):
                continue
            sid = r.get("gsis_id")
            if not sid:
                continue
            try:
                wk = int(r["week"])
            except Exception:
                continue
            stat.setdefault(str(sid), {})[wk] = r.get("status")
        back = {}
        for sid, wks in stat.items():
            order = sorted(wks)
            for i, wk in enumerate(order):
                if wks[wk] == "RES":
                    ir.setdefault(sid, set()).add(wk)
                    if i + 1 < len(order) and wks[order[i + 1]] == "ACT":
                        back.setdefault(sid, set()).add(order[i + 1])
        out = {"season": y, "ir": {k: sorted(v) for k, v in sorted(ir.items())},
               "back": {k: sorted(v) for k, v in sorted(back.items())}}
        json.dump(out, open(os.path.join(OUT, "%d.ir.json" % y), "w"), separators=(",", ":"))
        print(y, len(ir), "players")

if __name__ == "__main__":
    main()
