# Roblox Trend Tracker (draft v0.1)

Tracks every Roblox game at or above **300 concurrent players**. Each game is sorted by genre, niche and features, and the tracker flags rising games and niches early. It runs on GitHub Actions every hour, so **your PC never has to be on**, and every run commits its data to this repo.

## What it collects

| Source | What | How often |
|---|---|---|
| Roblox explore / Charts sorts | Which games are listed and at what rank (Top Playing, Up-and-Coming, genre sorts) | Every 6 h |
| Roblox search | Games matching the niche keywords in `config/settings.json` | Every 6 h |
| Roblox games API | CCU, visits, favorites, genre, subgenre, created/updated dates, description | Hourly |
| Roblox votes API | Likes / dislikes | Hourly |
| Roblox game passes + badges | Pass names and prices, badge award counts (a window into progression and monetization) | Daily, 300 games per day on rotation |
| Rolimons game list | Third-party player counts, used for discovery and as a cross-check | Hourly |
| YouTube Data API (optional) | Videos and views per game in the last 7 days ("coverage velocity") | Daily |

The other big trackers (RoMonitor, Rotrends, RoWatcher) have no public API, so they aren't scraped.

## Where the data goes

```
data/games.csv                  one row per tracked game (name, creator, genre, dates, description)
data/game_status.csv            live status per game (active, last/peak CCU, last seen)
data/snapshots/YYYY/MM/*.csv    every hourly reading, one file per day (gzipped after 3 days)
data/rollups/YYYY/MM/*.csv      daily avg/peak/low CCU, visits and favorites gained, like ratio
data/discovery/...              chart and search positions over time
data/tags.csv                   genre, niche and feature tags (rebuilt daily)
data/passes.csv, badges.csv     current passes and badges per game
data/third_party/rolimons/...   Rolimons readings
data/youtube/...                coverage numbers
data/runs.csv                   log of every step, success or failure
reports/watchlist.md            the daily trend report (young breakouts, rising niches, coverage spikes, movers)
reports/game_metrics.csv        per-game signals
reports/niche_metrics.csv       per-niche size, growth, saturation
```

**If a run is missed:** GitHub occasionally skips scheduled runs. The next run catches up every day's rollup, reports and daily tasks automatically. The one thing that can't be recovered is CCU for the missed hours, because Roblox only reports live numbers. Visits and favorites are running totals, so their daily gains stay correct.

## Setup (about 10 minutes)

1. Create a new GitHub repository and upload this folder's contents. Keep the `.github` folder.
   - **Public repo:** Actions minutes are free and unlimited, so it runs hourly.
   - **Private repo:** you get 2,000 free minutes a month. In `.github/workflows/collect.yml`, change the cron to `17 */2 * * *` (every 2 hours).
2. Go to **Settings → Actions → General → Workflow permissions** and choose **Read and write**.
3. Go to the **Actions** tab, enable workflows, open **collect**, click **Run workflow** and type `probe`. This checks every endpoint once and commits `reports/probe.md`. If anything says FAILED, send me that file.
4. Run it again with `run`. After that it runs on its own every hour.
5. Optional: add a free YouTube Data API key as a repository secret named `YOUTUBE_API_KEY` (Settings → Secrets and variables → Actions).

## Using the data on your PC

```
git pull
python -m tracker build-db     # creates tracker.db (SQLite) from all the CSVs
python -m tracker report       # rebuild the watchlist locally
```

Open `tracker.db` in any SQLite viewer, or have Claude query it.

## Tuning

- `config/settings.json`: CCU floor, polling cadence, search keywords, trigger thresholds.
- `config/niches.json`: niche and feature keyword rules.
- `config/tags_manual.json`: fix any game's tags by hand.
- `config/watchlist_manual.json`: force games onto the watchlist.

## Notes

- Growth signals switch on after 14 days of data, and niche trends after 14–28 days.
- Roblox doesn't document the explore and search endpoints. Parsing is defensive, and `probe` shows their live shape.
- The tracker makes read-only requests to public endpoints and throttles to about 2 per second. Roblox's Terms restrict automated data gathering, especially for AI training. Keep this data out of any model training, and expect that Roblox could rate-limit the runner.
- Tests: `python -m unittest discover -s tests -v`. This simulates 16 days against a fake Roblox, including a missed day.
