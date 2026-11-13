# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Pulls run FIT files off a Garmin watch over USB (MTP), flattens them to CSV, builds a markdown digest and an HTML dashboard per person, and can send the digest to Claude for a coaching review. macOS only (uses `ioreg` to read the watch serial).

## Commands

Setup: `brew install libmtp` (provides `mtp-filetree`, `mtp-getfile`, and `libmtp.dylib`, which the `Watch` class loads via ctypes), then `.venv/bin/pip install -r requirements.txt`.

```
.venv/bin/python garmin.py <profile>          # pull + parse into data/<profile>/
.venv/bin/python garmin.py <profile> pull     # watch -> data/<profile>/raw/*.fit, new files only
.venv/bin/python garmin.py <profile> parse    # raw -> runs.csv, laps.csv, records/*.csv, digest.md, dashboard.html
.venv/bin/python garmin.py <profile> coach "optional note"   # review -> data/<profile>/coach.md
.venv/bin/python garmin.py <profile> workouts  # coach.md plan -> workout FIT files -> watch GARMIN/NewFiles
.venv/bin/python garmin.py <profile> wipe      # remove every workout this script put on the watch
.venv/bin/python garmin.py serve              # dashboard on http://127.0.0.1:8765
```

There is no test suite or linter. `demo()` at the bottom of `garmin.py` holds assert-based self-checks and runs on every invocation, before anything else. Add checks for new parsing or metric logic there. To run only the checks: `.venv/bin/python -c "import garmin; garmin.demo()"`.

`parse` can be re-run at any time against existing `raw/` files without a watch attached.

## Layout

Everything is in `garmin.py`. `dashboard.html` is a template, not a page: `render()` substitutes the literal markers `/*DATA*/[]`, `/*ZONES*/[]` and `/*NAME*/` and writes the result to `data/<profile>/dashboard.html`. Keep those markers intact when editing the template. The page renders fine with empty data, which `serve` relies on for a profile with no runs yet.

`data/` is gitignored and holds personal data. Per profile it contains `raw/`, the CSVs, `digest.md`, `dashboard.html`, `watch.txt` (serial of the watch first pulled into it), `goal.txt`, `notes.md` (runner notes appended on each coach call), `coach.md` (last review, the one fed into the next prompt), `coach/<YYYY-MM-DD-HHMMSS>.md` (every review, browsable in the dashboard), `coach_request.md` (the full prompt last sent), `workouts/` (the plan as `plan.json` plus one `<date>.fit` per session).

## Things that span several functions

- **Profile state is global.** `set_profile()` sets the module globals `DATA` and `RAW`; `pull`, `parse`, `coach` and the writers all read them. The server takes a single lock around pull/parse/coach for this reason. Profile names must match `\w+` and cannot be a subcommand name.
- **One HR zone ruler for all runs.** `parse()` decodes every file first, takes the HR zone boundaries from the newest run, then computes zone times for every run from the per-second records with those boundaries (`run_metrics`). The watch's own per-file time-in-zone is deliberately ignored, because the watch redefines zones over time and the trends would not be comparable. `coach_prompt.md` explains this to the model; keep both in sync if it changes.
- **Walking is cadence-based.** `WALK_CADENCE` (strides/min, one foot) splits walking from running. Walk %, longest unbroken run, zone paces and drift all depend on it.
- **Wrong-watch guards.** `pull()` refuses to pull if the connected watch serial differs from `watch.txt`; `parse()` warns if the raw files come from more than one watch.
- **Coach review** shells out to the logged-in `claude` CLI (`claude -p --model $COACH_MODEL`), not the API. The prompt is `coach_prompt.md` plus today's date, goal, notes, the previous review and the digest. Changing the digest's columns means checking that `coach_prompt.md` still describes them correctly.
- **Workouts** (`workouts`, and the Push button in the coach dialog): `coach_prompt.md` makes the coach write a Watch column in its plan table in a fixed step format (`5 walk, 3x(5 run <155, 2 walk), 4 walk`). `plan_workouts()` and `watch_steps()` parse it, `workout_fit()` encodes each session with the SDK's `Encoder`, and a `Watch` session (libmtp through ctypes) copies the files to `GARMIN/NewFiles`. The watch imports them when unplugged and saves each as `GARMIN/Workouts/MM-DD-YY_<name>_workout.fit` (`_01`, `_02`... on re-import; older pushes lack the year). `planned_workouts()` recognises these by the `PLANNED` pattern. After a successful copy, the ones for days before today or days the new plan replaces are deleted in the same session. `wipe` (Remove button at the bottom of the coach dialog) deletes all of them plus any still waiting in `NewFiles`. Garmin's own workouts have no date prefix and are never touched. Custom HR targets are stored as bpm + 100. Changing the step format means changing `coach_prompt.md` and `WATCH_TOKEN` together.
- **MTP object ids are per session.** The watch renumbers its files on every new MTP connection, so an id from `mtp-filetree` or one `Watch` block must never be used in another. Doing so deleted the wrong ids once. `mtp-delfile` and `mtp-sendfile` each open their own session, so don't use them for workouts (`mtp-sendfile` also can't resolve a Garmin folder's storage). `pull` still uses `mtp-getfile` with ids from `mtp-filetree`; it has worked so far, but it carries the same risk.
- **Server API** (`serve`): `GET /api/profiles`, `GET /api/goal/<p>`, `GET /api/reviews/<p>` (all reviews, newest first), `GET /api/progress` (`[done, total]` of the running pull), `POST /api/{pull,coach,goal,workouts,wipe}/<p>`. POSTs require the header `X-Garmin: 1` as a CSRF guard; the dashboard's JS sends it. It binds to 127.0.0.1 only.
