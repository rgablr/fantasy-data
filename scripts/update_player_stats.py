#!/usr/bin/env python3
"""Download nflverse weekly player stats, trim to fantasy-relevant columns,
map nflverse player IDs to Sleeper IDs, and write compact JSON into /data.

Sources:
  - nflverse-data "stats_player" release:
    https://github.com/nflverse/nflverse-data/releases/tag/stats_player
    file: stats_player_week_<season>.csv.gz
  - Sleeper's public player list (has a matching gsis_id field):
    https://api.sleeper.app/v1/players/nfl

Stdlib only, no third-party dependencies.
"""

import csv
import gzip
import io
import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timezone

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(REPO_ROOT, "data")

NFLVERSE_RELEASE_BASE = "https://github.com/nflverse/nflverse-data/releases/download/stats_player"
SLEEPER_PLAYERS_URL = "https://api.sleeper.app/v1/players/nfl"
# Sleeper's own player list only backfills gsis_id for a subset of players
# (e.g. it's null for Justin Herbert, Jordan Love, Trevor Lawrence...), so the
# primary ID crosswalk is dynastyprocess's community-maintained mapping,
# which both nflverse and sleeper IDs more completely. Sleeper's list is kept
# as a fallback for any gsis_id it has that the crosswalk is missing.
DYNASTYPROCESS_IDS_URL = "https://raw.githubusercontent.com/dynastyprocess/data/master/files/db_playerids.csv"

# Columns that are genuinely continuous/decimal in the source data.
FLOAT_FIELDS = {
    "passing_epa", "passing_cpoe", "pacr",
    "rushing_epa",
    "receiving_epa", "racr", "target_share", "air_yards_share", "wopr",
    "fantasy_points", "fantasy_points_ppr",
}

STR_FIELDS = {
    "player_id", "player_name", "player_display_name",
    "position", "position_group", "team", "opponent_team",
    "season_type", "game_id",
}

# Columns kept from the raw nflverse file, in output order. Trimmed from the
# source's ~150 columns down to what's useful for fantasy scoring/analysis;
# dropped things like per-distance FG/punt breakdowns and yardage-bucket
# splits (passing_10/20/40, etc).
KEEP_COLUMNS = [
    "player_id", "player_name", "player_display_name", "position", "position_group",
    "team", "opponent_team", "season", "week", "season_type", "game_id",
    # passing
    "completions", "attempts", "passing_yards", "passing_tds", "passing_interceptions",
    "sacks_suffered", "sack_yards_lost", "passing_air_yards", "passing_yards_after_catch",
    "passing_first_downs", "passing_epa", "passing_cpoe", "passing_2pt_conversions", "pacr",
    # rushing
    "carries", "rushing_yards", "rushing_tds", "rushing_fumbles_lost",
    "rushing_first_downs", "rushing_epa", "rushing_2pt_conversions",
    # receiving
    "receptions", "targets", "receiving_yards", "receiving_tds", "receiving_fumbles_lost",
    "receiving_air_yards", "receiving_yards_after_catch", "receiving_first_downs",
    "receiving_epa", "receiving_2pt_conversions", "racr", "target_share", "air_yards_share", "wopr",
    # special teams / return
    "special_teams_tds", "punt_return_yards", "kickoff_return_yards",
    # kicking
    "fg_made", "fg_att", "fg_missed", "fg_blocked", "fg_long",
    "pat_made", "pat_att", "pat_missed", "gwfg_made",
    # defense / IDP
    "def_sacks", "def_interceptions", "def_fumbles_forced", "def_tds", "def_safeties",
    "def_pass_defended", "def_tackles_solo", "def_tackles_with_assist", "def_tackles_for_loss",
    "def_qb_hits", "fumble_recovery_tds",
    # fumbles (overall, any position)
    "fumbles_lost_total",
    # fantasy points (already computed by nflverse)
    "fantasy_points", "fantasy_points_ppr",
]


def log(msg):
    print(msg, flush=True)


def fetch_bytes(url):
    req = urllib.request.Request(url, headers={"User-Agent": "fantasy-data-updater"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read()


def current_season(today=None):
    """NFL season year: Jan/Feb still belong to the season that started the previous fall."""
    today = today or datetime.now(timezone.utc)
    return today.year - 1 if today.month <= 2 else today.year


def load_sleeper_id_map():
    mapping = {}

    log("Downloading dynastyprocess player ID crosswalk...")
    raw = fetch_bytes(DYNASTYPROCESS_IDS_URL)
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8")))
    for row in reader:
        gsis_id, sleeper_id = row.get("gsis_id"), row.get("sleeper_id")
        if gsis_id and sleeper_id:
            mapping[gsis_id] = sleeper_id
    log(f"  -> {len(mapping)} gsis_id -> sleeper_id pairs from crosswalk")

    log("Downloading Sleeper player list (fallback for crosswalk gaps)...")
    raw = fetch_bytes(SLEEPER_PLAYERS_URL)
    players = json.loads(raw)
    added = 0
    for sleeper_id, p in players.items():
        gsis_id = p.get("gsis_id")
        if gsis_id and gsis_id not in mapping:
            mapping[gsis_id] = sleeper_id
            added += 1
    log(f"  -> added {added} more pairs, {len(mapping)} total")
    return mapping


def convert_value(col, value):
    if value is None or value == "":
        return None
    if col in FLOAT_FIELDS:
        try:
            return round(float(value), 4)
        except ValueError:
            return None
    if col in STR_FIELDS:
        return value
    try:
        return int(value)
    except ValueError:
        try:
            return float(value)
        except ValueError:
            return value


def download_season_stats(season):
    url = f"{NFLVERSE_RELEASE_BASE}/stats_player_week_{season}.csv.gz"
    log(f"Downloading {url}")
    try:
        raw = fetch_bytes(url)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            log(f"  -> not found (no data published yet for season {season})")
            return None
        raise
    text = gzip.decompress(raw).decode("utf-8")
    rows = list(csv.DictReader(io.StringIO(text)))
    log(f"  -> {len(rows)} rows")
    return rows


def trim_row(row, id_map):
    out = {col: convert_value(col, row.get(col)) for col in KEEP_COLUMNS}
    out["sleeper_id"] = id_map.get(row.get("player_id"))
    return out


def write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, separators=(",", ":"), ensure_ascii=False)


def process_season(season, id_map):
    rows = download_season_stats(season)
    if rows is None:
        return None

    by_week = {}
    for row in rows:
        trimmed = trim_row(row, id_map)
        by_week.setdefault(trimmed["week"], []).append(trimmed)

    season_dir = os.path.join(DATA_DIR, "weekly", str(season))
    weeks = sorted(by_week)
    for week in weeks:
        write_json(os.path.join(season_dir, f"{week}.json"), by_week[week])
    if weeks:
        write_json(os.path.join(season_dir, "latest.json"), by_week[weeks[-1]])
    log(f"Season {season}: wrote weeks {weeks}")
    return weeks


def main():
    seasons_env = os.environ.get("SEASONS")
    if seasons_env:
        seasons = [int(s) for s in seasons_env.split(",") if s.strip()]
    else:
        season = current_season()
        seasons = [season - 1, season]

    id_map = load_sleeper_id_map()
    write_json(os.path.join(DATA_DIR, "player_id_map.json"), id_map)

    meta = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "https://github.com/nflverse/nflverse-data (release: stats_player)",
        "seasons": {},
    }
    for season in seasons:
        weeks = process_season(season, id_map)
        if weeks is not None:
            meta["seasons"][str(season)] = weeks

    write_json(os.path.join(DATA_DIR, "meta.json"), meta)
    log("Done.")


if __name__ == "__main__":
    main()
