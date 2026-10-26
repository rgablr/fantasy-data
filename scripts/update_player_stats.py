#!/usr/bin/env python3
"""Download nflverse weekly player stats and snap counts, trim to
fantasy-relevant columns, map nflverse player IDs to Sleeper IDs, and write
compact JSON into /data.

Sources:
  - nflverse-data "stats_player" release:
    https://github.com/nflverse/nflverse-data/releases/tag/stats_player
    file: stats_player_week_<season>.csv.gz
    player ID column: player_id (gsis_id)
  - nflverse-data "snap_counts" release:
    https://github.com/nflverse/nflverse-data/releases/tag/snap_counts
    file: snap_counts_<season>.csv.gz
    player ID column: pfr_player_id (Pro Football Reference ID)
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
from collections import Counter
from datetime import datetime, timezone

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(REPO_ROOT, "data")

NFLVERSE_RELEASE_BASE = "https://github.com/nflverse/nflverse-data/releases/download/stats_player"
SNAP_COUNTS_RELEASE_BASE = "https://github.com/nflverse/nflverse-data/releases/download/snap_counts"
SLEEPER_PLAYERS_URL = "https://api.sleeper.app/v1/players/nfl"
# Sleeper's own player list only backfills gsis_id for a subset of players
# (e.g. it's null for Justin Herbert, Jordan Love, Trevor Lawrence...), so the
# primary ID crosswalk is dynastyprocess's community-maintained mapping,
# which has both nflverse (gsis_id), PFR (pfr_id) and sleeper IDs more
# completely. Sleeper's list is kept as a fallback for any gsis_id it has
# that the crosswalk is missing. dynastyprocess uses the literal string "NA"
# for missing IDs rather than an empty field, so that value must be filtered
# out explicitly wherever these columns are read.
DYNASTYPROCESS_IDS_URL = "https://raw.githubusercontent.com/dynastyprocess/data/master/files/db_playerids.csv"

# Columns that are genuinely continuous/decimal in the source data.
FLOAT_FIELDS = {
    "passing_epa", "passing_cpoe", "pacr",
    "rushing_epa",
    "receiving_epa", "racr", "target_share", "air_yards_share", "wopr",
    "fantasy_points", "fantasy_points_ppr",
    "offense_pct",
}

# Identity/context columns in KEEP_COLUMNS, as opposed to the stat columns
# that get aggregated (summed/averaged) into the season file.
IDENTITY_COLUMNS = {
    "player_id", "player_name", "player_display_name", "position", "position_group",
    "team", "opponent_team", "season", "week", "season_type", "game_id",
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


def _valid_id(value):
    return bool(value) and value != "NA"


def load_id_maps():
    """Returns (gsis_id -> sleeper_id, pfr_id -> sleeper_id)."""
    gsis_map = {}
    pfr_map = {}

    log("Downloading dynastyprocess player ID crosswalk...")
    raw = fetch_bytes(DYNASTYPROCESS_IDS_URL)
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8")))
    for row in reader:
        sleeper_id = row.get("sleeper_id")
        if not _valid_id(sleeper_id):
            continue
        if _valid_id(row.get("gsis_id")):
            gsis_map[row["gsis_id"]] = sleeper_id
        if _valid_id(row.get("pfr_id")):
            pfr_map[row["pfr_id"]] = sleeper_id
    log(f"  -> {len(gsis_map)} gsis_id and {len(pfr_map)} pfr_id -> sleeper_id pairs from crosswalk")

    log("Downloading Sleeper player list (fallback for gsis_id crosswalk gaps)...")
    raw = fetch_bytes(SLEEPER_PLAYERS_URL)
    players = json.loads(raw)
    added = 0
    for sleeper_id, p in players.items():
        # Sleeper pads some gsis ids with a leading space (2019 class)
        gsis_id = (p.get("gsis_id") or "").strip()
        if gsis_id and gsis_id not in gsis_map:
            gsis_map[gsis_id] = sleeper_id
            added += 1
    log(f"  -> added {added} more gsis_id pairs, {len(gsis_map)} total")
    return gsis_map, pfr_map


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


def download_snap_counts(season):
    url = f"{SNAP_COUNTS_RELEASE_BASE}/snap_counts_{season}.csv.gz"
    log(f"Downloading {url}")
    try:
        raw = fetch_bytes(url)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            log(f"  -> not found (no snap count data published yet for season {season})")
            return []
        raise
    text = gzip.decompress(raw).decode("utf-8")
    rows = list(csv.DictReader(io.StringIO(text)))
    log(f"  -> {len(rows)} rows")
    return rows


def build_snap_lookup(snap_rows, pfr_map):
    """Maps (sleeper_id, week) -> (offense_snaps, offense_pct), and reports
    crosswalk coverage since snap_counts IDs players by pfr_id rather than
    the gsis_id the rest of this script keys off."""
    lookup = {}
    positions_by_pfr_id = {}
    matched_pfr_ids = set()
    for row in snap_rows:
        pfr_id = row.get("pfr_player_id")
        positions_by_pfr_id[pfr_id] = row.get("position")
        sleeper_id = pfr_map.get(pfr_id)
        if not sleeper_id:
            continue
        matched_pfr_ids.add(pfr_id)
        week = convert_value("week", row.get("week"))
        offense_snaps = convert_value("offense_snaps", row.get("offense_snaps"))
        offense_pct = convert_value("offense_pct", row.get("offense_pct"))
        lookup[(sleeper_id, week)] = (offense_snaps, offense_pct)

    total = len(positions_by_pfr_id)
    log(f"  -> mapped {len(matched_pfr_ids)}/{total} unique snap-count players to sleeper_id")
    unmatched_positions = Counter(
        pos for pfr_id, pos in positions_by_pfr_id.items() if pfr_id not in matched_pfr_ids
    )
    if unmatched_positions:
        breakdown = ", ".join(f"{pos}:{n}" for pos, n in unmatched_positions.most_common())
        log(f"     unmapped positions: {breakdown}")
    return lookup


def trim_row(row, id_map, snap_lookup):
    out = {col: convert_value(col, row.get(col)) for col in KEEP_COLUMNS}
    sleeper_id = id_map.get(row.get("player_id"))
    out["sleeper_id"] = sleeper_id
    offense_snaps, offense_pct = snap_lookup.get((sleeper_id, out["week"]), (None, None))
    out["offense_snaps"] = offense_snaps
    out["offense_pct"] = offense_pct
    return out


def build_season_summary(by_week):
    """One row per player: counting stats summed, rate stats averaged, plus
    games played, total offensive snaps, and average snap percentage."""
    rate_columns = sorted(FLOAT_FIELDS - {"fantasy_points", "fantasy_points_ppr", "offense_pct"})
    max_columns = ["fg_long"]
    sum_columns = [
        c for c in KEEP_COLUMNS
        if c not in IDENTITY_COLUMNS and c not in rate_columns and c not in max_columns
    ]

    rows_by_player = {}
    for week in sorted(by_week):
        for row in by_week[week]:
            rows_by_player.setdefault(row["player_id"], []).append(row)

    season_rows = []
    for rows in rows_by_player.values():
        rows.sort(key=lambda r: r["week"])
        last = rows[-1]
        out = {
            "sleeper_id": last.get("sleeper_id"),
            "name": last.get("player_display_name"),
            "position": last.get("position"),
            "team": last.get("team"),
            "games_played": len(rows),
        }
        for col in sum_columns + ["offense_snaps"]:
            total = sum(r.get(col) or 0 for r in rows)
            out[col] = round(total, 4) if isinstance(total, float) else total
        for col in rate_columns + ["offense_pct"]:
            vals = [r[col] for r in rows if r.get(col) is not None]
            out[col] = round(sum(vals) / len(vals), 4) if vals else None
        for col in max_columns:
            vals = [r[col] for r in rows if r.get(col) is not None]
            out[col] = max(vals) if vals else None
        season_rows.append(out)
    return season_rows


def write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, separators=(",", ":"), ensure_ascii=False)


def process_season(season, id_map, snap_lookup):
    rows = download_season_stats(season)
    if rows is None:
        return None

    by_week = {}
    for row in rows:
        trimmed = trim_row(row, id_map, snap_lookup)
        by_week.setdefault(trimmed["week"], []).append(trimmed)

    season_dir = os.path.join(DATA_DIR, "weekly", str(season))
    weeks = sorted(by_week)
    for week in weeks:
        write_json(os.path.join(season_dir, f"{week}.json"), by_week[week])
    if weeks:
        write_json(os.path.join(season_dir, "latest.json"), by_week[weeks[-1]])
    log(f"Season {season}: wrote weeks {weeks}")
    return by_week


def main():
    seasons_env = os.environ.get("SEASONS")
    if seasons_env:
        seasons = [int(s) for s in seasons_env.split(",") if s.strip()]
    else:
        season = current_season()
        seasons = [season - 1, season]

    id_map, pfr_map = load_id_maps()
    write_json(os.path.join(DATA_DIR, "player_id_map.json"), id_map)

    meta = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "https://github.com/nflverse/nflverse-data (releases: stats_player, snap_counts)",
        "seasons": {},
    }
    for season in seasons:
        snap_rows = download_snap_counts(season)
        snap_lookup = build_snap_lookup(snap_rows, pfr_map) if snap_rows else {}

        by_week = process_season(season, id_map, snap_lookup)
        if by_week is None:
            continue
        meta["seasons"][str(season)] = sorted(by_week)

        season_summary = build_season_summary(by_week)
        write_json(os.path.join(DATA_DIR, "season", f"{season}.json"), season_summary)
        log(f"Season {season}: wrote season totals for {len(season_summary)} players")

    write_json(os.path.join(DATA_DIR, "meta.json"), meta)
    log("Done.")


if __name__ == "__main__":
    main()
