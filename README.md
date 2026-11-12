# garm-insights

Reads your runs straight off a Garmin watch over USB and turns them into a training log you own. No Garmin Connect account, no cloud sync. You plug in the watch, it copies the new activity files, and you get CSVs, a markdown digest, a local dashboard and, if you want it, a written review from Claude acting as your running coach.

I built it to track my own progress toward a sub-20 5K. Several people can share it, each with their own profile.

## What you get

For each profile, in `data/<profile>/`:

- `runs.csv`, `laps.csv` and `records/<file>.csv` with every field the watch recorded, per run, per lap and per second.
- `digest.md`, a weekly summary plus one row per run, written to be read in one pass by you or a model.
- `dashboard.html`, with weekly minutes split by effort, a per-run trend chart where you pick the measure, pace in each heart rate zone, and your latest runs.
- `coach.md`, the latest coaching review with a two-week plan.

The script computes a few numbers the watch doesn't show you directly:

- Time in each heart rate zone, recomputed for every run with your current zone limits. The watch shifts its zones as your fitness changes, so its own per-run figures can't be compared across months. These can.
- Walk share and longest unbroken stretch of running, using cadence to tell running from walking. Useful if you still take walk breaks.
- Pace while in zone 2, the plainest sign of aerobic progress.
- Heart rate drift, how much speed per heartbeat drops between the first and second half of a run.

## Requirements

- macOS. The script uses `ioreg` to read the watch's serial number.
- Python 3.10 or newer.
- libmtp for talking to the watch: `brew install libmtp`.
- The [Claude Code](https://claude.ai/code) CLI, logged in, if you want coach reviews. Everything else works without it.

```
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Usage

Plug in the watch, unlock it, and quit Garmin Express or Android File Transfer if they're running, since they hold the USB connection.

```
.venv/bin/python garmin.py robin          # copy new runs off the watch and rebuild everything
.venv/bin/python garmin.py robin pull     # only copy new files
.venv/bin/python garmin.py robin parse    # only rebuild from files already copied
.venv/bin/python garmin.py robin coach    # ask Claude for a review
.venv/bin/python garmin.py serve          # open the dashboard at http://127.0.0.1:8765
```

Replace `robin` with any profile name made of letters, digits and underscores. The first pull into a profile records the watch's serial number, and later pulls stop if a different watch is plugged in, so one person's runs don't end up in someone else's log.

The dashboard server has buttons for fetching new runs, switching profiles, editing your goal and requesting a coach review, so after setup you rarely need the command line. It only listens on localhost.

## Coach reviews

`coach` sends Claude the prompt in `coach_prompt.md` along with your goal, your notes, the previous review and the digest. It writes the answer to `coach.md`.

Write your goal in `data/<profile>/goal.txt` or on the dashboard. To tell the coach something the watch can't see, add a note:

```
.venv/bin/python garmin.py robin coach "left calf tight after Sunday's run"
```

Notes are kept in `notes.md` and included in every later review. The full prompt is saved to `coach_request.md` each time, so you can paste it into another model if you'd rather.

The review runs on Sonnet by default. Change `COACH_MODEL` at the top of `garmin.py` to `"opus"` if Sonnet's reviews fall short.

## Privacy

Everything under `data/` stays on your machine and is gitignored. That includes GPS tracks, heart rate traces and the watch serial. The only thing that leaves your computer is the coach prompt, and only when you ask for a review.

## License

MIT
