#!/usr/bin/env python3
"""Build weekly injury-report files for QB/RB/WR/TE from the nflverse injury reports (2009 onward).

Writes data/hist/<Y>.inj.json for each season Y:
  {"season": Y, "inj": {"<gsis_id>": {"<week>": {"st": report status, "pi": primary injury, "ps": practice status,
                                                  "pr": practice primary injury}}}}
Regular season only. Statuses as nflverse spells them (Out, Doubtful, Questionable, Probable; empty when the player was
listed with a practice status only). Players are keyed by gsis_id (the app maps a Sleeper player to it).

Source: nflverse-data release "injuries" (injuries_<year>.csv).

Usage:
  python3 build_injuries.py              # 2009-2025
  python3 build_injuries.py 2024         # one season
  python3 build_injuries.py 2020 2025    # inclusive range

Stdlib only.
"""
import csv, io, json, os, sys, urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(ROOT, "data", "hist")
BASE = "https://github.com/nflverse/nflverse-data/releases/download/injuries/injuries_%d.csv"
FIRST, LAST = 2009, 2025
POS = {"QB", "RB", "WR", "TE"}
KEEP = (("st", "report_status"), ("pi", "report_primary_injury"), ("ps", "practice_status"), ("pr", "practice_primary_injury"))


def clean(v):
    v = (v or "").strip()
    return v if v and v.lower() != "nan" else ""


def main():
    a = [int(x) for x in sys.argv[1:]]
    y0, y1 = (a[0], a[0]) if len(a) == 1 else (a[0], a[1]) if len(a) == 2 else (FIRST, LAST)
    os.makedirs(OUT, exist_ok=True)
    for y in range(y0, y1 + 1):
        try:
            raw = urllib.request.urlopen(BASE % y, timeout=120).read().decode("utf-8")
        except Exception as e:
            print(y, "skipped:", e); continue
        inj = {}
        for r in csv.DictReader(io.StringIO(raw)):
            if r.get("position") not in POS or r.get("game_type") != "REG":
                continue
            sid = clean(r.get("gsis_id"))
            if not sid:
                continue
            try:
                wk = int(r["week"])
            except Exception:
                continue
            rec = {k: clean(r.get(src)) for k, src in KEEP}
            if not any(rec.values()):
                continue
            inj.setdefault(sid, {})[str(wk)] = {k: v for k, v in rec.items() if v}
        out = {"season": y, "inj": {k: dict(sorted(v.items(), key=lambda kv: int(kv[0]))) for k, v in sorted(inj.items())}}
        json.dump(out, open(os.path.join(OUT, "%d.inj.json" % y), "w"), separators=(",", ":"))
        print(y, len(inj), "players", sum(len(v) for v in inj.values()), "player-weeks")


if __name__ == "__main__":
    main()
