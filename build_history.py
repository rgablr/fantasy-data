#!/usr/bin/env python3
"""Build historical season/week stat files for the Stats Playground from
nflverse-data, for every regular-season QB/RB/WR/TE from 1999 onward.

Writes, for each season Y:
  data/hist/<Y>.json       - one row per qualifying player, season totals
  data/hist/<Y>.week.json  - the same stats broken out by week

Sources (nflverse-data GitHub releases):
  - stats_player/stats_player_week_<year>.csv
    (falls back to player_stats/player_stats_<year>.csv if missing)
  - rosters/roster_<year>.csv
    (jersey_number, headshot_url, espn_id, pfr_id, keyed by gsis_id)
  - snap_counts/snap_counts_<year>.csv (2012 onward only)
    (keyed by pfr_player_id, joined via roster's pfr_id)

Usage:
  python3 build_history.py                 # 1999-2025
  python3 build_history.py 2010             # just 2010
  python3 build_history.py 2010 2015        # 2010-2015 inclusive

Stdlib only, no third-party dependencies.
"""

import csv
import gzip
import io
import json
import os
import sys
import urllib.error
import urllib.request
from collections import Counter, defaultdict

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(REPO_ROOT, "data", "hist")

RELEASE_BASE = "https://github.com/nflverse/nflverse-data/releases/download"
DEFAULT_FIRST_YEAR = 1999
DEFAULT_LAST_YEAR = 2025
SNAP_COUNTS_FIRST_YEAR = 2012

POSITIONS = {"QB", "RB", "WR", "TE"}

# player_stats_<year>.csv (the fallback source) uses a handful of different
# column names for the same data as stats_player_week_<year>.csv; normalize
# to the primary schema's names so the rest of the script doesn't care which
# source a given year came from.
FALLBACK_COLUMN_RENAME = {
    "recent_team": "team",
    "interceptions": "passing_interceptions",
    "sacks": "sacks_suffered",
    "sack_yards": "sack_yards_lost",
}

# Historical team abbreviations: nflverse labels every season with the
# franchise's current abbreviation, so relabel seasons before each move.
def normalize_team(team, season):
    if team == "LA" and season < 2016:
        return "STL"
    if team == "LAC" and season < 2017:
        return "SD"
    if team == "LV" and season < 2020:
        return "OAK"
    return team


# sleeper-style season/week key -> source column, summed as-is
S_SUM_COLUMNS = {
    "pass_cmp": "completions",
    "pass_att": "attempts",
    "pass_yd": "passing_yards",
    "pass_td": "passing_tds",
    "pass_int": "passing_interceptions",
    "pass_sack": "sacks_suffered",
    "pass_2pt": "passing_2pt_conversions",
    "rush_att": "carries",
    "rush_yd": "rushing_yards",
    "rush_td": "rushing_tds",
    "rush_2pt": "rushing_2pt_conversions",
    "rec_tgt": "targets",
    "rec": "receptions",
    "rec_yd": "receiving_yards",
    "rec_td": "receiving_tds",
    "rec_2pt": "receiving_2pt_conversions",
}

# nflverse columns kept as-is (summed over the season/week), always present
# (defaulted to 0 in the source, never blank).
X_SUM_COLUMNS = [
    "passing_air_yards", "passing_yards_after_catch", "passing_first_downs",
    "rushing_fumbles_lost", "rushing_first_downs", "rushing_2pt_conversions",
    "receiving_fumbles_lost", "receiving_air_yards", "receiving_yards_after_catch",
    "receiving_first_downs", "special_teams_tds", "punt_return_yards",
    "kickoff_return_yards", "fumble_recovery_tds",
]
# summed, but blank in the source (and thus skipped) for weeks with no
# qualifying attempt/carry/target
X_EPA_SUM_COLUMNS = ["passing_epa", "rushing_epa", "receiving_epa"]
# averaged (not summed) over weeks where present
X_AVG_COLUMNS = ["passing_cpoe"]


def log(msg):
    print(msg, flush=True)


def fetch_text(url):
    req = urllib.request.Request(url, headers={"User-Agent": "fantasy-data-history-builder"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def fetch_csv_rows(url):
    text = fetch_text(url)
    if text is None:
        return None
    return list(csv.DictReader(io.StringIO(text)))


def download_stats(year):
    url = f"{RELEASE_BASE}/stats_player/stats_player_week_{year}.csv"
    log(f"Downloading {url}")
    rows = fetch_csv_rows(url)
    if rows is not None:
        log(f"  -> {len(rows)} rows")
        return rows

    url = f"{RELEASE_BASE}/player_stats/player_stats_{year}.csv"
    log(f"  -> not found, falling back to {url}")
    rows = fetch_csv_rows(url)
    if rows is None:
        log(f"  -> not found either; skipping season {year}")
        return []
    for row in rows:
        for old, new in FALLBACK_COLUMN_RENAME.items():
            if old in row:
                row[new] = row.pop(old)
    log(f"  -> {len(rows)} rows (fallback source)")
    return rows


def download_roster(year):
    url = f"{RELEASE_BASE}/rosters/roster_{year}.csv"
    log(f"Downloading {url}")
    rows = fetch_csv_rows(url) or []
    log(f"  -> {len(rows)} rows")
    return rows


def download_snap_counts(year):
    if year < SNAP_COUNTS_FIRST_YEAR:
        return []
    url = f"{RELEASE_BASE}/snap_counts/snap_counts_{year}.csv"
    log(f"Downloading {url}")
    rows = fetch_csv_rows(url) or []
    log(f"  -> {len(rows)} rows")
    return rows


def download_pbp_targets(year):
    """(gsis_id, week) -> [targets, air_yards] from nflverse play-by-play:
    regular-season pass attempts thrown to a receiver (no sacks, no 2-point
    tries), the same rule the dynasty-tool ratings use. Streamed, not saved."""
    url = f"{RELEASE_BASE}/pbp/play_by_play_{year}.csv.gz"
    log(f"Downloading {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "fantasy-data-history-builder"})
    out = defaultdict(lambda: [0, 0])
    with urllib.request.urlopen(req, timeout=300) as resp:
        text = io.TextIOWrapper(gzip.GzipFile(fileobj=resp), encoding="utf-8")
        for row in csv.DictReader(text):
            if row.get("season_type") != "REG" or not row.get("posteam") or num(row.get("two_point_attempt")) == 1:
                continue
            if num(row.get("pass_attempt")) != 1 or num(row.get("sack")) == 1:
                continue
            gsis_id, week = row.get("receiver_id") or row.get("receiver_player_id"), num(row.get("week"))
            if not gsis_id or week is None:
                continue
            entry = out[(gsis_id, week)]
            entry[0] += 1
            entry[1] += num(row.get("air_yards")) or 0
    log(f"  -> {len(out)} player-weeks with targets")
    return out


def fill_targets(year, stats_rows):
    """2003-2008: the weekly stats files have no targets (and no air yards
    before 2006), so take them from play-by-play for those seasons only."""
    # Covered = most player-weeks with a catch also have the stat (a few
    # stray values, like 2003's 3 players with targets, don't count).
    rows = [r for r in stats_rows if r.get("season_type") == "REG"]
    caught = [r for r in rows if (num(r.get("receptions")) or 0) > 0]
    has_tgt = sum(1 for r in caught if num(r.get("targets"))) >= 0.9 * len(caught)
    has_ay = sum(1 for r in caught if num(r.get("receiving_air_yards"))) >= 0.5 * len(caught)
    if has_tgt and has_ay:
        return
    pbp = download_pbp_targets(year)
    pbp_ay = any(v[1] for v in pbp.values())  # play-by-play air yards start in 2006
    for r in rows:
        tgt, ay = pbp.get((r.get("player_id"), num(r.get("week"))), (0, 0))
        if not has_tgt:
            r["targets"] = tgt
        if not has_ay and pbp_ay:
            r["receiving_air_yards"] = ay


def check_targets(year, season_players):
    """Fail if a season has receivers but (almost) no targets, as 2003-2008 did."""
    rec = sum(1 for p in season_players if p["s"].get("rec", 0) > 0)
    tgt = sum(1 for p in season_players if p["s"].get("rec_tgt", 0) > 0)
    if rec >= 100 and tgt < 0.9 * rec:
        sys.exit(f"Season {year}: {rec} players with receptions but only {tgt} with targets; files not written")


def num(value):
    """String -> int/float, treating blank as missing (None)."""
    if value is None or value == "":
        return None
    try:
        f = float(value)
    except ValueError:
        return None
    return int(f) if f == int(f) else f


def clean_num(value):
    """Round floats and collapse whole numbers to int, to keep files small."""
    if value is None:
        return None
    if isinstance(value, float):
        value = round(value, 4)
        if value == int(value):
            return int(value)
    return value


def build_roster_lookup(roster_rows):
    """gsis_id -> {jersey_number, headshot_url, espn_id, pfr_id}, and the
    reverse pfr_id -> gsis_id map used to join snap_counts rows."""
    by_gsis = {}
    pfr_to_gsis = {}
    for row in roster_rows:
        gsis_id = row.get("gsis_id")
        if not gsis_id:
            continue
        entry = by_gsis.setdefault(gsis_id, {})
        for field in ("jersey_number", "headshot_url", "espn_id", "pfr_id"):
            value = row.get(field)
            if value:
                entry[field] = value
        pfr_id = row.get("pfr_id")
        if pfr_id:
            pfr_to_gsis[pfr_id] = gsis_id
    return by_gsis, pfr_to_gsis


def build_snap_data(snap_rows, pfr_to_gsis):
    """(gsis_id, week) -> (offense_snaps, offense_pct), and
    (team, week) -> max offense_snaps seen on that team that week (i.e. the
    team's total offensive snap count for the game)."""
    player_week_snaps = {}
    team_week_max = {}
    for row in snap_rows:
        week = num(row.get("week"))
        team = row.get("team")
        offense_snaps = num(row.get("offense_snaps"))
        if week is not None and team and offense_snaps is not None:
            key = (team, week)
            if offense_snaps > team_week_max.get(key, 0):
                team_week_max[key] = offense_snaps

        gsis_id = pfr_to_gsis.get(row.get("pfr_player_id"))
        if gsis_id and week is not None:
            offense_pct = num(row.get("offense_pct"))
            player_week_snaps[(gsis_id, week)] = (offense_snaps, offense_pct)
    return player_week_snaps, team_week_max


def collect_player_weeks(stats_rows):
    """Groups qualifying rows (REG season, weeks 1-18, QB/RB/WR/TE) by
    gsis_id -> week -> row. Also tracks each player's display name and
    the raw (un-normalized) team they played for each week."""
    weeks_by_player = defaultdict(dict)
    names = {}
    teams_by_week = defaultdict(dict)

    for row in stats_rows:
        if row.get("season_type") != "REG":
            continue
        week = num(row.get("week"))
        if week is None or not (1 <= week <= 18):
            continue
        position = row.get("position")
        if position not in POSITIONS:
            continue
        gsis_id = row.get("player_id")
        if not gsis_id:
            continue

        weeks_by_player[gsis_id][week] = row
        names[gsis_id] = row.get("player_display_name") or row.get("player_name") or names.get(gsis_id)
        teams_by_week[gsis_id][week] = row.get("team")

    return weeks_by_player, names, teams_by_week


def sum_column(weeks, rows_by_week, column):
    return sum(num(rows_by_week[w].get(column)) or 0 for w in weeks)


def season_snap_totals(gsis_id, weeks, teams_by_week, player_week_snaps, team_week_max):
    off_snp = 0
    tm_off_snp = 0
    found = False
    for week in weeks:
        snap = player_week_snaps.get((gsis_id, week))
        if snap is not None and snap[0] is not None:
            off_snp += snap[0]
            found = True
        team = teams_by_week[week]
        tm_max = team_week_max.get((team, week))
        if tm_max is not None:
            tm_off_snp += tm_max
            found = True
    if not found:
        return None, None
    return off_snp, tm_off_snp


def build_season_stats(gsis_id, weeks, rows_by_week, teams_by_week, player_week_snaps, team_week_max):
    s = {}
    for key, column in S_SUM_COLUMNS.items():
        s[key] = sum_column(weeks, rows_by_week, column)
    s["fum_lost"] = (
        sum_column(weeks, rows_by_week, "rushing_fumbles_lost")
        + sum_column(weeks, rows_by_week, "receiving_fumbles_lost")
        + sum_column(weeks, rows_by_week, "sack_fumbles_lost")
    )
    s["gp"] = len(weeks)

    off_snp, tm_off_snp = season_snap_totals(gsis_id, weeks, teams_by_week, player_week_snaps, team_week_max)
    if off_snp is not None:
        s["off_snp"] = off_snp
        s["tm_off_snp"] = tm_off_snp

    x = {}
    x["sack_yards_lost"] = sum(abs(num(rows_by_week[w].get("sack_yards_lost")) or 0) for w in weeks)
    for column in X_SUM_COLUMNS:
        x[column] = sum_column(weeks, rows_by_week, column)
    for column in X_EPA_SUM_COLUMNS:
        vals = [num(rows_by_week[w].get(column)) for w in weeks]
        vals = [v for v in vals if v is not None]
        if vals:
            x[column] = sum(vals)
    for column in X_AVG_COLUMNS:
        vals = [num(rows_by_week[w].get(column)) for w in weeks]
        vals = [v for v in vals if v is not None]
        if vals:
            x[column] = sum(vals) / len(vals)

    s = {k: clean_num(v) for k, v in s.items()}
    x = {k: clean_num(v) for k, v in x.items()}
    return s, x


def build_week_stats(gsis_id, week, row, player_week_snaps, team_week_max, team):
    s = {}
    for key, column in S_SUM_COLUMNS.items():
        s[key] = num(row.get(column)) or 0
    s["fum_lost"] = (
        (num(row.get("rushing_fumbles_lost")) or 0)
        + (num(row.get("receiving_fumbles_lost")) or 0)
        + (num(row.get("sack_fumbles_lost")) or 0)
    )
    s["gp"] = 1

    snap = player_week_snaps.get((gsis_id, week))
    if snap is not None and snap[0] is not None:
        s["off_snp"] = snap[0]
    tm_max = team_week_max.get((team, week))
    if tm_max is not None:
        s["tm_off_snp"] = tm_max

    n = {}
    n["sack_yards_lost"] = abs(num(row.get("sack_yards_lost")) or 0)
    for column in X_SUM_COLUMNS:
        n[column] = num(row.get(column)) or 0
    for column in X_EPA_SUM_COLUMNS + X_AVG_COLUMNS:
        value = num(row.get(column))
        if value is not None:
            n[column] = value

    s = {k: clean_num(v) for k, v in s.items()}
    n = {k: clean_num(v) for k, v in n.items()}
    # Drop zero values, keep gp.
    s = {k: v for k, v in s.items() if k == "gp" or v != 0}
    n = {k: v for k, v in n.items() if v != 0}
    return s, n


def build_face(gsis_id, rows_by_week, weeks, roster_entry):
    for week in reversed(weeks):
        headshot = rows_by_week[week].get("headshot_url")
        if headshot:
            return headshot
    if roster_entry.get("headshot_url"):
        return roster_entry["headshot_url"]
    espn_id = roster_entry.get("espn_id")
    if espn_id:
        return f"https://a.espncdn.com/i/headshots/nfl/players/full/{espn_id}.png"
    return None


def process_year(year):
    stats_rows = download_stats(year)
    if not stats_rows:
        return None
    fill_targets(year, stats_rows)

    roster_rows = download_roster(year)
    roster_lookup, pfr_to_gsis = build_roster_lookup(roster_rows)

    snap_rows = download_snap_counts(year)
    player_week_snaps, team_week_max = build_snap_data(snap_rows, pfr_to_gsis)

    weeks_by_player, names, teams_by_week = collect_player_weeks(stats_rows)

    season_players = []
    week_entries = {}

    for gsis_id, rows_by_week in weeks_by_player.items():
        weeks = sorted(rows_by_week)
        usage = sum(
            (num(rows_by_week[w].get("attempts")) or 0)
            + (num(rows_by_week[w].get("carries")) or 0)
            + (num(rows_by_week[w].get("targets")) or 0)
            # receptions count too, in case a season's targets are missing
            + (num(rows_by_week[w].get("receptions")) or 0)
            for w in weeks
        )
        if usage == 0:
            continue

        team_counts = Counter(teams_by_week[gsis_id][w] for w in weeks)
        season_team = normalize_team(team_counts.most_common(1)[0][0], year)
        position = rows_by_week[weeks[-1]].get("position")

        s, x = build_season_stats(
            gsis_id, weeks, rows_by_week, teams_by_week[gsis_id], player_week_snaps, team_week_max
        )

        player = {
            "i": gsis_id,
            "n": names.get(gsis_id),
            "p": position,
            "t": season_team,
        }
        roster_entry = roster_lookup.get(gsis_id, {})
        jersey = roster_entry.get("jersey_number")
        if jersey:
            player["j"] = int(float(jersey))
        face = build_face(gsis_id, rows_by_week, weeks, roster_entry)
        if face:
            player["f"] = face
        player["s"] = s
        player["x"] = x
        season_players.append(player)

        player_weeks = {}
        for week in weeks:
            team = teams_by_week[gsis_id][week]
            ws, wn = build_week_stats(gsis_id, week, rows_by_week[week], player_week_snaps, team_week_max, team)
            player_weeks[str(week)] = {"s": ws, "n": wn}
        week_entries[gsis_id] = player_weeks

    check_targets(year, season_players)
    os.makedirs(OUT_DIR, exist_ok=True)
    season_path = os.path.join(OUT_DIR, f"{year}.json")
    with open(season_path, "w", encoding="utf-8") as f:
        json.dump({"season": year, "players": season_players}, f, separators=(",", ":"), ensure_ascii=False)

    week_path = os.path.join(OUT_DIR, f"{year}.week.json")
    with open(week_path, "w", encoding="utf-8") as f:
        json.dump({"season": year, "weeks": week_entries}, f, separators=(",", ":"), ensure_ascii=False)

    log(f"Season {year}: wrote {len(season_players)} players")
    return len(season_players)


def main():
    args = sys.argv[1:]
    if len(args) == 0:
        first_year, last_year = DEFAULT_FIRST_YEAR, DEFAULT_LAST_YEAR
    elif len(args) == 1:
        first_year = last_year = int(args[0])
    else:
        first_year, last_year = int(args[0]), int(args[1])

    for year in range(first_year, last_year + 1):
        process_year(year)

    log("Done.")


if __name__ == "__main__":
    main()
