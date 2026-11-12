#!/usr/bin/env python3
"""Pull activity FIT files off a Garmin watch (MTP) and flatten the runs to CSV.

    .venv/bin/python garmin.py robin         # pull + parse into data/robin/
    .venv/bin/python garmin.py robin pull    # watch -> data/robin/raw/*.fit (only new files)
    .venv/bin/python garmin.py robin parse   # raw -> runs.csv, laps.csv, records/*.csv

One profile per person; plug in one watch at a time and name whose it is.

Needs: brew install libmtp; pip install garmin-fit-sdk
"""
import csv
import json
import re
from collections import defaultdict
from datetime import datetime, timedelta
import subprocess
import sys
from pathlib import Path

DATA = RAW = None  # set per profile in __main__
SEMI = 180 / 2**31  # FIT stores lat/long in semicircles
WALK_CADENCE = 65  # strides/min (one foot), so 130 steps/min. Below this counts as walking; tune per runner.


def activity_files(tree: str) -> dict[str, str]:
    """Parse `mtp-filetree` output -> {filename: object_id} for the Activity folder."""
    out, inside = {}, False
    for line in tree.splitlines():
        m = re.match(r"^( *)(\d+) (.+)$", line)
        if not m:
            continue
        indent, oid, name = len(m[1]), m[2], m[3]
        if indent == 2:
            inside = name == "Activity"
        elif inside and indent == 4 and name.lower().endswith(".fit"):
            out[name] = oid
    return out


def pull():
    RAW.mkdir(parents=True, exist_ok=True)
    tree = subprocess.run(["mtp-filetree"], capture_output=True, text=True).stdout
    files = activity_files(tree)
    if not files:
        sys.exit("No activity files found. Is the watch plugged in and unlocked? (Close Garmin Express / Android File Transfer.)")
    todo = {n: i for n, i in files.items() if not (RAW / n).exists()}
    print(f"{len(files)} on watch, {len(todo)} new")
    for k, (name, oid) in enumerate(sorted(todo.items(), reverse=True), 1):  # newest first
        part = RAW / (name + ".part")  # rename only when complete, so a yanked cable can't leave a half file
        subprocess.run(["mtp-getfile", oid, str(part)], capture_output=True)
        if part.exists() and part.stat().st_size:
            part.rename(RAW / name)
            print(f"[{k}/{len(todo)}] {name}")
        else:
            part.unlink(missing_ok=True)
            print(f"[{k}/{len(todo)}] {name} FAILED")


def flat(mesg: dict) -> dict:
    """Keep named scalar fields; drop the SDK's unknown numeric-keyed ones."""
    return {k: v for k, v in mesg.items() if isinstance(k, str) and not isinstance(v, (list, dict))}


def add_pace(row: dict) -> dict:
    d, t = row.get("total_distance"), row.get("total_timer_time")
    if d and t:
        row["distance_km"] = round(d / 1000, 3)
        row["pace_min_per_km"] = round(t / 60 / (d / 1000), 2)
    return row


def run_metrics(recs: list[dict], top: list) -> dict:
    """Per-second trace -> time in HR zones, walk share, longest unbroken run, pace at zone 2 HR, pace:HR drift.

    top[i] is the upper bpm of zone i. The caller passes the same boundaries for every run.
    """
    moving = walk = longest = cur = z2_t = z2_d = 0.0
    z2 = (top[1], top[2]) if len(top) > 2 else None
    zone_s = [0.0] * (len(top) + 1)
    running = []  # (speed, hr) while running, for drift
    for a, b in zip(recs, recs[1:]):
        dt = min((b["timestamp"] - a["timestamp"]).total_seconds(), 10)  # cap pauses
        cad, v, hr = a.get("cadence"), a.get("enhanced_speed"), a.get("heart_rate")
        if hr and top:
            zone_s[next((i for i, t in enumerate(top) if hr <= t), len(top))] += dt
        if not cad:  # standing still
            cur = 0
            continue
        moving += dt
        if z2 and hr and v and z2[0] < hr <= z2[1]:
            z2_t += dt
            z2_d += v * dt
        if cad < WALK_CADENCE:
            walk += dt
            cur = 0
        else:
            cur += dt
            longest = max(longest, cur)
            if v and hr:
                running.append((v, hr))
    out = {"longest_run_min": round(longest / 60, 1)}
    out.update({f"hr_zone_{i}_s": round(s) for i, s in enumerate(zone_s)} if top else {})
    if moving:
        out["walk_pct"] = round(100 * walk / moving, 1)
    if z2_t >= 120:
        out["z2_pace_min_per_km"] = round(z2_t / 60 / (z2_d / 1000), 2)
    if len(running) >= 600:  # drift means little on anything shorter than ~10 min
        h = len(running) // 2
        ef = [sum(v for v, _ in part) / sum(hr for _, hr in part) for part in (running[:h], running[h:])]
        out["hr_drift_pct"] = round(100 * (ef[0] / ef[1] - 1), 1)  # positive = slower per heartbeat in 2nd half
    return out


def write_digest(runs: list[dict], top: list):
    """One markdown file a coach (or Claude) can read in a single pass."""
    def g(r, k, fmt="{}"):
        return fmt.format(r[k]) if r.get(k) is not None else "-"

    def zone_pct(r, zones):
        tot = sum(r.get(f"hr_zone_{i}_s", 0) for i in range(7))
        return f"{100 * sum(r.get(f'hr_zone_{i}_s', 0) for i in zones) / tot:.0f}" if tot else "-"

    weeks = defaultdict(list)
    for r in runs:
        d = r["start_time"].date()
        weeks[d - timedelta(days=d.weekday())].append(r)
    L = [f"# Running digest: {DATA.name}", "",
         f"Generated {datetime.now():%Y-%m-%d}. {len(runs)} runs. Times are UTC. Walking = cadence under {WALK_CADENCE * 2} steps/min.",
         f"HR zones for every run use the newest run's boundaries (upper bpm of zones 0-5: {top}), not the zones set on the watch at the time.",
         "", "## Weeks", "", "| week of | runs | min | km | longest unbroken run (min) |", "|---|---|---|---|---|"]
    for w in sorted(weeks):
        rs = weeks[w]
        L.append(f"| {w} | {len(rs)} | {sum(r['total_timer_time'] for r in rs) / 60:.0f} | "
                 f"{sum(r.get('distance_km', 0) for r in rs):.1f} | {max(r['longest_run_min'] for r in rs)} |")
    L += ["", "## Runs", "",
          "| date | type | min | km | pace /km | avg HR | max HR | %Z1-2 | %Z3 | %Z4-5 | walk % | longest unbroken (min) | Z2 pace /km | drift % | steps/min | power W | aerobic TE |",
          "|" + "---|" * 17]
    for r in runs:
        cad = r.get("avg_running_cadence")
        L.append(f"| {r['start_time']:%Y-%m-%d} | {g(r, 'sub_sport')} | {r['total_timer_time'] / 60:.0f} | {g(r, 'distance_km', '{:.2f}')} | "
                 f"{g(r, 'pace_min_per_km')} | {g(r, 'avg_heart_rate')} | {g(r, 'max_heart_rate')} | {zone_pct(r, (0, 1, 2))} | {zone_pct(r, (3,))} | "
                 f"{zone_pct(r, (4, 5, 6))} | {g(r, 'walk_pct')} | {g(r, 'longest_run_min')} | {g(r, 'z2_pace_min_per_km')} | "
                 f"{g(r, 'hr_drift_pct')} | {cad * 2 if cad else '-'} | {g(r, 'avg_power')} | {g(r, 'total_training_effect')} |")
    L += ["", "Paces are decimal minutes (6.50 = 6:30). Drift: positive means less speed per heartbeat in the second half.",
          "Per-lap detail is in laps.csv, per-second traces in records/<file>.csv.", ""]
    (DATA / "digest.md").write_text("\n".join(L))


def write_dashboard(runs: list[dict]):
    """Fill dashboard.html (the template next to this script) with this profile's runs."""
    zmin = lambda r, zones: round(sum(r.get(f"hr_zone_{i}_s", 0) for i in zones) / 60, 1)
    rows = [{"date": r["start_time"].isoformat(), "min": round(r["total_timer_time"] / 60, 1), "km": r.get("distance_km", 0),
             "pace": r.get("pace_min_per_km"), "hr": r.get("avg_heart_rate"),
             "easy": zmin(r, (0, 1, 2)), "mod": zmin(r, (3,)), "hard": zmin(r, (4, 5, 6)),
             "longest": r.get("longest_run_min"), "walk": r.get("walk_pct"), "z2pace": r.get("z2_pace_min_per_km")} for r in runs]
    tpl = (Path(__file__).parent / "dashboard.html").read_text()
    (DATA / "dashboard.html").write_text(tpl.replace("/*DATA*/[]", json.dumps(rows)).replace("/*NAME*/", DATA.name))


def write_csv(path: Path, rows: list[dict], first: list[str]):
    keys = first + sorted({k for r in rows for k in r} - set(first))
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, keys)
        w.writeheader()
        w.writerows(rows)


def parse():
    from garmin_fit_sdk import Decoder, Stream

    (DATA / "records").mkdir(parents=True, exist_ok=True)
    runs, laps, skipped, watches, decoded, top = [], [], {}, set(), [], []
    for fit in sorted(RAW.glob("*.fit")):
        m, errors = Decoder(Stream.from_file(str(fit))).read()
        sess = (m.get("session_mesgs") or [{}])[0]
        watches.add((m.get("file_id_mesgs") or [{}])[0].get("serial_number"))
        if errors or sess.get("sport") != "running":
            key = "decode error" if errors else str(sess.get("sport"))
            skipped[key] = skipped.get(key, 0) + 1
            continue
        for z in m.get("time_in_zone_mesgs", []):
            if z.get("reference_mesg") == "session" and z.get("hr_zone_high_boundary"):
                top = z["hr_zone_high_boundary"]  # ends up as the newest run's zones
        decoded.append((fit, m, sess))
    # The watch re-derives zones over time, so each file's own time-in-zone uses a different ruler.
    # Measure every run against the newest boundaries instead, so trends are comparable.
    for fit, m, sess in decoded:
        run = add_pace({"file": fit.stem, **flat(sess)})
        laps += [add_pace({"file": fit.stem, "lap": i, **flat(l)}) for i, l in enumerate(m.get("lap_mesgs", []), 1)]
        recs = [flat(r) for r in m.get("record_mesgs", [])]
        run.update(run_metrics(recs, top))
        runs.append(run)
        for r in recs:
            for k in ("position_lat", "position_long"):
                if r.get(k) is not None:
                    r[k] = round(r[k] * SEMI, 6)
        write_csv(DATA / "records" / f"{fit.stem}.csv", recs, ["timestamp"])
    write_csv(DATA / "runs.csv", runs, ["file", "start_time", "sub_sport", "distance_km", "pace_min_per_km", "total_timer_time", "avg_heart_rate", "longest_run_min", "walk_pct", "z2_pace_min_per_km", "hr_drift_pct"])
    write_digest(runs, top)
    write_dashboard(runs)
    write_csv(DATA / "laps.csv", laps, ["file", "lap", "start_time", "distance_km", "pace_min_per_km", "total_timer_time", "avg_heart_rate"])
    print(f"{len(runs)} runs, {len(laps)} laps -> {DATA}/  (skipped: {skipped})")
    if len(watches) > 1:
        print(f"WARNING: files from {len(watches)} different watches in this profile {watches}. Wrong watch pulled into it?")


def demo():
    tree = "Storage: Internal\n16 GARMIN\n  20 Sports\n    21 Run.fit\n  30 Activity\n    31 2026-05-07-08-11-04.fit\n    32 notes.txt\n  40 Workouts\n    41 w.fit\n"
    assert activity_files(tree) == {"2026-05-07-08-11-04.fit": "31"}
    assert add_pace({"total_distance": 5000, "total_timer_time": 1500})["pace_min_per_km"] == 5.0
    # 5 min running in zone 2 at 2.5 m/s, 1 min walking, 3 min running
    t0 = datetime(2026, 1, 1)
    rec = lambda i, cad: {"timestamp": t0 + timedelta(seconds=i), "cadence": cad, "enhanced_speed": 2.5, "heart_rate": 140}
    recs = [rec(i, 80) for i in range(300)] + [rec(i, 55) for i in range(300, 360)] + [rec(i, 80) for i in range(360, 541)]
    m = run_metrics(recs, [110, 130, 150, 170, 190])
    assert m["longest_run_min"] == 5.0 and m["walk_pct"] == 11.1 and m["z2_pace_min_per_km"] == 6.67 and m["hr_zone_2_s"] == 540, m


if __name__ == "__main__":
    demo()
    if len(sys.argv) < 2 or not re.fullmatch(r"\w+", sys.argv[1]) or sys.argv[1] in ("pull", "parse"):
        sys.exit("usage: garmin.py <profile> [pull|parse]")
    DATA = Path(__file__).parent / "data" / sys.argv[1]
    RAW = DATA / "raw"
    cmd = sys.argv[2] if len(sys.argv) > 2 else "all"
    if cmd in ("pull", "all"):
        pull()
    if cmd in ("parse", "all"):
        parse()
