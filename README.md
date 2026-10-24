# fantasy-data
Stores fantasy data for a fantasy football league analyzer.

## Stats Playground history

`data/hist/<year>.json` (season totals) and `data/hist/<year>.week.json` (same
stats by week) cover every QB/RB/WR/TE regular season from 1999-2025, built
from nflverse-data by `build_history.py`.

To rebuild a single year (e.g. after nflverse corrects historical data):

```
python3 build_history.py 2016
```

Or a range, or the whole 1999-2025 history with no arguments:

```
python3 build_history.py 2010 2015
python3 build_history.py
```
