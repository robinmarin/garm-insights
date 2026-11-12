#!/usr/bin/env python3
"""Pull activity FIT files off a Garmin watch (MTP) and flatten the runs to CSV.

    .venv/bin/python garmin.py robin         # pull + parse into data/robin/
    .venv/bin/python garmin.py robin pull    # watch -> data/robin/raw/*.fit (only new files)
    .venv/bin/python garmin.py robin parse   # raw -> runs.csv, laps.csv, records/*.csv

    .venv/bin/python garmin.py serve         # dashboard at http://127.0.0.1:8765 with a fetch button

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

ROOT = Path(__file__).parent
DATA = RAW = None  # set by set_profile()


def set_profile(name: str):
    global DATA, RAW
    if not re.fullmatch(r"\w+", name) or name in ("pull", "parse", "serve"):
        sys.exit("usage: garmin.py <profile> [pull|parse]  |  garmin.py serve")
    DATA = ROOT / "data" / name
    RAW = DATA / "raw"


def watch_serial(out: str | None = None) -> str | None:
    """Serial of the plugged-in Garmin (USB vendor 0x091e), from macOS. None if it can't be read."""
    if out is None:
        out = subprocess.run(["ioreg", "-r", "-c", "IOUSBHostDevice", "-l", "-w", "0"], capture_output=True, text=True).stdout
    for device in re.split(r"(?m)^\+-o ", out):  # one block per top-level USB device
        m = re.search(r'"USB Serial Number" = "([^"]+)"', device)
        if '"idVendor" = 2334' in device and m:
            return m[1]
    return None
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
    # Each profile remembers its watch, so plugging in the other person's can't mix two people's runs.
    serial, known = watch_serial(), DATA / "watch.txt"
    if serial and known.exists() and known.read_text().strip() != serial:
        sys.exit(f"The connected watch ({serial}) is not the one profile '{DATA.name}' was pulled from before. Pick the right profile.")
    tree = subprocess.run(["mtp-filetree"], capture_output=True, text=True).stdout
    files = activity_files(tree)
    if not files:
        sys.exit("No activity files found. Is the watch plugged in and unlocked? (Close Garmin Express / Android File Transfer.)")
    RAW.mkdir(parents=True, exist_ok=True)
    if serial:
        known.write_text(serial)
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
    moving = walk = longest = cur = 0.0
    zone_s = [0.0] * (len(top) + 1)  # all time with a heart rate
    zone_t, zone_d = list(zone_s), list(zone_s)  # time and distance while moving, for pace per zone
    running = []  # (speed, hr) while running, for drift
    for a, b in zip(recs, recs[1:]):
        dt = min((b["timestamp"] - a["timestamp"]).total_seconds(), 10)  # cap pauses
        cad, v, hr = a.get("cadence"), a.get("enhanced_speed"), a.get("heart_rate")
        z = next((i for i, t in enumerate(top) if hr <= t), len(top)) if hr and top else None
        if z is not None:
            zone_s[z] += dt
        if not cad:  # standing still
            cur = 0
            continue
        moving += dt
        if z is not None and v:
            zone_t[z] += dt
            zone_d[z] += v * dt
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
    for i, (t, d) in enumerate(zip(zone_t, zone_d)):
        if t >= 120 and d:  # under 2 minutes in a zone is too little to call a pace
            out[f"z{i}_pace_min_per_km"] = round(t / 60 / (d / 1000), 2)
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


def write_dashboard(runs: list[dict], top: list):
    """Fill dashboard.html (the template next to this script) with this profile's runs."""
    rows = [{"date": r["start_time"].isoformat(), "min": round(r["total_timer_time"] / 60, 1), "km": r.get("distance_km", 0),
             "pace": r.get("pace_min_per_km"), "hr": r.get("avg_heart_rate"),
             "zm": [round(r.get(f"hr_zone_{i}_s", 0) / 60, 1) for i in range(7)],  # minutes in zones 0-6
             "zp": [r.get(f"z{i}_pace_min_per_km") for i in range(6)],  # pace in zones 0-5
             "longest": r.get("longest_run_min"), "walk": r.get("walk_pct"),
             "cad": r["avg_running_cadence"] * 2 if r.get("avg_running_cadence") else None} for r in runs]
    (DATA / "dashboard.html").write_text(render(rows, DATA.name, top))


def render(rows: list, name: str, top: list = ()) -> str:
    tpl = (ROOT / "dashboard.html").read_text()
    return tpl.replace("/*DATA*/[]", json.dumps(rows)).replace("/*ZONES*/[]", json.dumps(list(top))).replace("/*NAME*/", name)


def serve(port=8765):
    """Local-only server: shows each profile's dashboard and lets its button run pull + parse."""
    import contextlib
    import io
    import threading
    import webbrowser
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    busy = threading.Lock()  # pull/parse use module globals, so one fetch at a time

    class H(BaseHTTPRequestHandler):
        def reply(self, body, ctype="application/json"):
            body = body.encode() if isinstance(body, str) else json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            data = ROOT / "data"
            profiles = sorted(d.name for d in data.iterdir() if d.is_dir()) if data.exists() else []
            if self.path == "/api/profiles":
                return self.reply(profiles)
            m = re.fullmatch(r"/p/(\w+)", self.path)
            if not m:
                self.send_response(302)
                self.send_header("Location", f"/p/{profiles[0] if profiles else 'me'}")
                return self.end_headers()
            f = data / m[1] / "dashboard.html"
            self.reply(f.read_text() if f.exists() else render([], m[1]), "text/html; charset=utf-8")

        def do_POST(self):
            m = re.fullmatch(r"/api/pull/(\w+)", self.path)
            # The custom header can't be sent cross-site without a CORS preflight, so other web pages can't trigger a pull.
            if not m or self.headers.get("X-Garmin") != "1":
                return self.send_error(400)
            if not busy.acquire(blocking=False):
                return self.reply({"ok": False, "message": "A fetch is already running"})
            log = io.StringIO()
            try:
                with contextlib.redirect_stdout(log):
                    set_profile(m[1])
                    pull()
                    parse()
                lines = [l for l in log.getvalue().splitlines() if not l.startswith("[")]
                self.reply({"ok": True, "message": " · ".join(lines)})
            except SystemExit as e:
                self.reply({"ok": False, "message": str(e)})
            except Exception as e:
                self.reply({"ok": False, "message": f"{type(e).__name__}: {e}"})
            finally:
                busy.release()

    url = f"http://127.0.0.1:{port}/"
    print(f"Dashboard at {url}  (Ctrl-C to stop)")
    webbrowser.open(url)
    # Threaded because browsers hold idle connections open, which stalls a single-threaded server.
    ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()


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
    write_dashboard(runs, top)
    write_csv(DATA / "laps.csv", laps, ["file", "lap", "start_time", "distance_km", "pace_min_per_km", "total_timer_time", "avg_heart_rate"])
    print(f"{len(runs)} runs, {len(laps)} laps (skipped: {skipped})")
    if len(watches) > 1:
        print(f"WARNING: files from {len(watches)} different watches in this profile {watches}. Wrong watch pulled into it?")


def demo():
    tree = "Storage: Internal\n16 GARMIN\n  20 Sports\n    21 Run.fit\n  30 Activity\n    31 2026-05-07-08-11-04.fit\n    32 notes.txt\n  40 Workouts\n    41 w.fit\n"
    assert activity_files(tree) == {"2026-05-07-08-11-04.fit": "31"}
    usb = '+-o Hub@1  <class IOUSBHostDevice>\n  |   "idVendor" = 1452\n  |   "USB Serial Number" = "hub1"\n+-o Watch@2  <class IOUSBHostDevice>\n  |   "USB Serial Number" = "0000aa"\n  |   "idVendor" = 2334\n'
    assert watch_serial(usb) == "0000aa" and watch_serial(usb.replace("2334", "99")) is None
    assert add_pace({"total_distance": 5000, "total_timer_time": 1500})["pace_min_per_km"] == 5.0
    # 5 min running in zone 2 at 2.5 m/s, 1 min walking, 3 min running
    t0 = datetime(2026, 1, 1)
    rec = lambda i, cad: {"timestamp": t0 + timedelta(seconds=i), "cadence": cad, "enhanced_speed": 2.5, "heart_rate": 140}
    recs = [rec(i, 80) for i in range(300)] + [rec(i, 55) for i in range(300, 360)] + [rec(i, 80) for i in range(360, 541)]
    m = run_metrics(recs, [110, 130, 150, 170, 190])
    assert m["longest_run_min"] == 5.0 and m["walk_pct"] == 11.1 and m["z2_pace_min_per_km"] == 6.67 and m["hr_zone_2_s"] == 540 and "z3_pace_min_per_km" not in m, m


if __name__ == "__main__":
    demo()
    if sys.argv[1:] == ["serve"]:
        serve()
    set_profile(sys.argv[1] if len(sys.argv) > 1 else "")
    cmd = sys.argv[2] if len(sys.argv) > 2 else "all"
    if cmd in ("pull", "all"):
        pull()
    if cmd in ("parse", "all"):
        parse()
