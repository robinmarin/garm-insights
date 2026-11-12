#!/usr/bin/env python3
"""Pull activity FIT files off a Garmin watch (MTP) and flatten the runs to CSV.

    .venv/bin/python garmin.py robin         # pull + parse into data/robin/
    .venv/bin/python garmin.py robin pull    # watch -> data/robin/raw/*.fit (only new files)
    .venv/bin/python garmin.py robin parse   # raw -> runs.csv, laps.csv, records/*.csv

    .venv/bin/python garmin.py robin coach   # digest + coach_prompt.md -> Claude -> data/robin/coach.md

    .venv/bin/python garmin.py serve         # dashboard at http://127.0.0.1:8765 with a fetch button

One profile per person; plug in one watch at a time and name whose it is.

Needs: brew install libmtp; pip install garmin-fit-sdk
"""
import csv
import json
import re
from collections import defaultdict
from datetime import date, datetime, timedelta
import subprocess
import sys
import zlib
from pathlib import Path

COACH_MODEL = "sonnet"  # model alias for the coach review; "opus" if Sonnet's reviews fall short

ROOT = Path(__file__).parent
DATA = RAW = None  # set by set_profile()


def set_profile(name: str):
    global DATA, RAW
    if not re.fullmatch(r"\w+", name) or name in ("pull", "parse", "serve", "coach", "workouts", "wipe"):
        sys.exit("usage: garmin.py <profile> [pull|parse|coach|workouts|wipe]  |  garmin.py serve")
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


def folder_files(tree: str, folder: str) -> dict[str, str]:
    """Parse `mtp-filetree` output -> {filename: object_id} for the .fit files in GARMIN/<folder>."""
    out, inside = {}, False
    for line in tree.splitlines():
        m = re.match(r"^( *)(\d+) (.+)$", line)
        if not m:
            continue
        indent, oid, name = len(m[1]), m[2], m[3]
        if indent == 2:
            inside = name == folder
        elif inside and indent == 4 and name.lower().endswith(".fit"):
            out[name] = oid
    return out


def check_watch() -> str | None:
    """Each profile remembers its watch, so plugging in the other person's can't mix two people's runs or plans."""
    serial, known = watch_serial(), DATA / "watch.txt"
    if serial and known.exists() and known.read_text().strip() != serial:
        sys.exit(f"The connected watch ({serial}) is not the one profile '{DATA.name}' was pulled from before. Pick the right profile.")
    return serial


def pull():
    serial, known = check_watch(), DATA / "watch.txt"
    tree = subprocess.run(["mtp-filetree"], capture_output=True, text=True).stdout
    files = folder_files(tree, "Activity")
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
    """Local-only server: shows each profile's dashboard and lets its buttons run pull + parse and the coach review."""
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
            m = re.fullmatch(r"/api/goal/(\w+)", self.path)
            if m:
                f = data / m[1] / "goal.txt"
                return self.reply(f.read_text() if f.exists() else "", "text/plain; charset=utf-8")
            m = re.fullmatch(r"/api/reviews/(\w+)", self.path)
            if m:  # newest first
                files = sorted((data / m[1] / "coach").glob("*.md"), reverse=True)
                return self.reply([{"name": f.stem, "text": f.read_text()} for f in files])
            m = re.fullmatch(r"/p/(\w+)", self.path)
            if not m:
                self.send_response(302)
                self.send_header("Location", f"/p/{profiles[0] if profiles else 'me'}")
                return self.end_headers()
            f = data / m[1] / "dashboard.html"
            self.reply(f.read_text() if f.exists() else render([], m[1]), "text/html; charset=utf-8")

        def do_POST(self):
            m = re.fullmatch(r"/api/(pull|coach|goal|workouts|wipe)/(\w+)", self.path)
            # The custom header can't be sent cross-site without a CORS preflight, so other web pages can't trigger a pull.
            if not m or self.headers.get("X-Garmin") != "1":
                return self.send_error(400)
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode()
            if m[1] == "goal":
                (ROOT / "data" / m[2]).mkdir(parents=True, exist_ok=True)
                (ROOT / "data" / m[2] / "goal.txt").write_text(body.strip() + "\n")
                return self.reply({"ok": True, "message": "Goal saved"})
            if not busy.acquire(blocking=False):
                return self.reply({"ok": False, "message": "A fetch or review is already running"})
            log = io.StringIO()
            try:
                with contextlib.redirect_stdout(log):
                    set_profile(m[2])
                    if m[1] == "coach":
                        coach(body)
                    elif m[1] == "workouts":
                        workouts()
                    elif m[1] == "wipe":
                        wipe()
                    else:
                        pull()
                        parse()
                lines = [l for l in log.getvalue().splitlines() if not l.startswith("[")]
                done = {"coach": "Review ready", "workouts": lines[-1] if lines else "", "wipe": lines[-1] if lines else ""}
                self.reply({"ok": True, "message": done.get(m[1], " · ".join(lines))})
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


def ask_claude(prompt: str, sent: Path) -> str:
    """Send prompt to the logged-in claude CLI; the prompt is saved to `sent` so it can be pasted elsewhere."""
    sent.write_text(prompt + "\n")
    try:
        # ponytail: uses the logged-in claude CLI, so no API key or SDK. Swap for the anthropic SDK if this runs unattended.
        out = subprocess.run(["claude", "-p", "--model", COACH_MODEL], input=prompt, capture_output=True, text=True, timeout=900, cwd=DATA)
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        sys.exit(f"Could not reach Claude ({e}). The prompt is in {sent}.")
    if out.returncode or not out.stdout.strip():
        sys.exit(f"Claude failed: {out.stderr.strip() or 'empty answer'}. The prompt is in {sent}.")
    return out.stdout.strip()


def coach(note=""):
    """Wrap goal, the runner's notes, last review and digest in the coach prompt, send it to Claude, save the answer."""
    def part(title, path, missing):
        return f"\n\n## {title}\n\n{path.read_text().strip() if path.exists() else missing}"

    if not (DATA / "digest.md").exists():
        sys.exit(f"No digest for '{DATA.name}' yet. Run: garmin.py {DATA.name}")
    if note.strip():  # kept for every later review, so the coach plans around it from then on
        with open(DATA / "notes.md", "a") as f:
            f.write(f"### {datetime.now():%Y-%m-%d}\n\n{note.strip()}\n\n")
    prompt = ((ROOT / "coach_prompt.md").read_text().strip()
              + f"\n\n## Today\n\n{datetime.now():%A %Y-%m-%d}"
              + part("The runner's goal, in their words", DATA / "goal.txt",
                     "Not stated. Coach for general fitness, starting with 30 minutes of continuous easy running, and list this under What I assumed.")
              + part("The runner's notes, oldest first", DATA / "notes.md", "None yet.")
              + part("Your previous review", DATA / "coach.md", "None. This is the first review.")
              + part("Training log", DATA / "digest.md", ""))
    text = re.sub(r"\A#[^\n]*\n+", "", ask_claude(prompt, DATA / "coach_request.md"))  # it sometimes repeats the previous review's title
    now = datetime.now()
    review = f"# Coach review: {DATA.name}, {now:%Y-%m-%d}\n\n{text}\n"
    (DATA / "coach.md").write_text(review)  # the latest, which the next prompt includes
    (DATA / "coach").mkdir(exist_ok=True)
    (DATA / "coach" / f"{now:%Y-%m-%d-%H%M%S}.md").write_text(review)  # every review, for the dashboard's history
    print(text, f"\n\nSaved to {DATA / 'coach.md'}")


# One step in the coach table's Watch column: "3x(" opens a repeat, ")" closes it, "5 run 120-132" or "5 walk <155" is a timed step.
WATCH_TOKEN = re.compile(r"(\d+)x\(|(\d+(?:\.\d+)?) (walk|run)(?: (\d+)-(\d+)| <(\d+))?|\)")


def watch_steps(text: str) -> list[dict]:
    """'5 walk, 3x(5 run <155, 2 walk), 4 walk' -> workout_fit steps. Format defined in coach_prompt.md."""
    if re.sub(r"[\s,]", "", WATCH_TOKEN.sub("", text)):
        raise ValueError(f"can't read {text!r}")
    stack = [[]]
    for m in WATCH_TOKEN.finditer(text):
        if m[1]:
            block = {"repeat": int(m[1]), "steps": []}
            stack[-1].append(block)
            stack.append(block["steps"])
        elif m[2]:
            st = {"min": float(m[2]), "kind": "active" if m[3] == "run" else "recovery"}
            if m[4]:
                st["hr"] = [int(m[4]), int(m[5])]
            elif m[6]:  # ceiling only; 90 is a floor no runner drops below, so the low alarm never fires
                st["hr"] = [90, int(m[6])]
            stack[-1].append(st)
        elif len(stack) > 1:
            stack.pop()
        else:
            raise ValueError(f"unmatched ')' in {text!r}")
    steps = stack[0]
    if len(stack) > 1 or not steps:
        raise ValueError(f"can't read {text!r}")
    for i, kind in ((0, "warmup"), (-1, "cooldown")):  # a walk at either end is the warm-up or cool-down
        if steps[i].get("kind") == "recovery":
            steps[i]["kind"] = kind
    return steps


def plan_workouts(review: str) -> list[dict]:
    """The coach review's plan table (the one with a Watch column) -> [{"date", "name", "steps"}], rest days left out."""
    lines = [l.strip() for l in review.splitlines()]
    start = next((i for i, l in enumerate(lines) if l.startswith("|") and "Watch" in l), None)
    if start is None:
        raise ValueError("the review has no plan table with a Watch column")
    row = lambda l: [c.strip() for c in l.strip("|").split("|")]
    head = row(lines[start])
    d, n, w = head.index("Date"), head.index("Session"), head.index("Watch")
    plan = []
    for l in lines[start + 1:]:
        if not l.startswith("|"):
            break
        r = row(l)
        steps = r[w].strip("` ") if len(r) == len(head) else ""
        if not steps or set(r[0]) <= set("-: "):  # rest day or the |---| line
            continue
        date = re.search(r"\d{4}-\d\d-\d\d", r[d])
        if not date:
            raise ValueError(f"no YYYY-MM-DD date in {r[d]!r}")
        y, mo, dd = date[0].split("-")
        plan.append({"date": date[0], "name": f"{mo}-{dd}-{y[2:]} {r[n]}"[:24], "steps": watch_steps(steps)})
    return plan


def workout_fit(w: dict) -> bytes:
    """{"name", "steps"} -> a FIT workout file. A step is {"min", "kind", "hr": [lo, hi]} or {"repeat": n, "steps": [...]}."""
    from garmin_fit_sdk import Encoder

    rows = []

    def add(steps):
        for st in steps:
            if "repeat" in st:  # inner steps, then a step that jumps back to the first of them
                first = len(rows)
                add(st["steps"])
                rows.append({"duration_type": "repeat_until_steps_cmplt", "duration_value": first, "target_value": int(st["repeat"])})
                continue
            row = {"intensity": st.get("kind", "active"), "target_type": "open"}
            row.update({"duration_type": "time", "duration_value": round(st["min"] * 60000)} if st.get("min") else {"duration_type": "open"})
            if st.get("hr"):  # custom HR targets are stored as bpm + 100; 1-100 would mean % of max
                lo, hi = st["hr"]
                row.update(target_type="heart_rate", target_value=0, custom_target_value_low=int(lo) + 100, custom_target_value_high=int(hi) + 100)
            rows.append(row)

    add(w["steps"])
    e = Encoder()
    # The watch treats files with the same file_id as one file and imports only one of them, so each workout needs its own serial.
    serial = zlib.crc32(w["name"].encode()) or 1
    e.write_mesg({"mesg_num": 0, "type": "workout", "manufacturer": "development", "product": 0, "serial_number": serial, "time_created": datetime.now()})
    e.write_mesg({"mesg_num": 26, "sport": "running", "num_valid_steps": len(rows), "wkt_name": w["name"]})
    for i, row in enumerate(rows):
        e.write_mesg({"mesg_num": 27, "message_index": i, **row})
    return e.close()


class Watch:
    """One libmtp session with the plugged-in watch: find folders, send, list and delete by object id.

    A Garmin renumbers its objects on every new MTP session, so ids from mtp-filetree or an earlier session can point
    at a different file. Look ids up and use them inside the same `with Watch()` block, never across two.
    mtp-sendfile is no help either: it finds the folder but not its storage, so the device rejects the upload.
    """
    ROOT = 0xFFFFFFFF

    def __enter__(self):
        import ctypes as C

        class F(C.Structure):  # LIBMTP_file_t
            pass
        F._fields_ = [("item_id", C.c_uint32), ("parent_id", C.c_uint32), ("storage_id", C.c_uint32), ("filename", C.c_char_p),
                      ("filesize", C.c_uint64), ("modificationdate", C.c_long), ("filetype", C.c_int), ("next", C.POINTER(F))]

        class Storage(C.Structure):  # LIBMTP_devicestorage_t, only the id we need
            _fields_ = [("id", C.c_uint32)]

        class Device(C.Structure):  # LIBMTP_mtpdevice_t, up to the storage list
            _fields_ = [("object_bitsize", C.c_uint8), ("params", C.c_void_p), ("usbinfo", C.c_void_p), ("storage", C.POINTER(Storage))]

        lib = C.CDLL("/opt/homebrew/lib/libmtp.dylib")
        lib.LIBMTP_Open_Raw_Device_Uncached.restype = C.POINTER(Device)
        lib.LIBMTP_Open_Raw_Device_Uncached.argtypes = [C.c_void_p]
        lib.LIBMTP_Get_Files_And_Folders.restype = C.POINTER(F)
        lib.LIBMTP_Get_Files_And_Folders.argtypes = [C.POINTER(Device), C.c_uint32, C.c_uint32]
        lib.LIBMTP_destroy_file_t.argtypes = [C.POINTER(F)]
        lib.LIBMTP_Send_File_From_File.argtypes = [C.POINTER(Device), C.c_char_p, C.POINTER(F), C.c_void_p, C.c_void_p]
        lib.LIBMTP_Delete_Object.argtypes = [C.POINTER(Device), C.c_uint32]
        lib.LIBMTP_Release_Device.argtypes = [C.POINTER(Device)]
        lib.LIBMTP_Init()
        raw, n = C.c_void_p(), C.c_int()
        # Uncached, because a cached device refuses to list one folder at a time.
        self.dev = lib.LIBMTP_Open_Raw_Device_Uncached(raw) if not lib.LIBMTP_Detect_Raw_Devices(C.byref(raw), C.byref(n)) and n.value else None
        if not self.dev or not self.dev.contents.storage:
            sys.exit("Could not open the watch over MTP. Unlock it, close Garmin Express / Android File Transfer, and try again.")
        self.C, self.F, self.lib, self.storage = C, F, lib, self.dev.contents.storage.contents.id
        return self

    def __exit__(self, *exc):
        self.lib.LIBMTP_Release_Device(self.dev)

    def ls(self, folder: int) -> dict[str, int]:
        """{name: object_id} of everything directly inside a folder."""
        out, f = {}, self.lib.LIBMTP_Get_Files_And_Folders(self.dev, self.storage, folder)
        while f:
            out[f.contents.filename.decode()] = f.contents.item_id
            nxt = f.contents.next
            self.lib.LIBMTP_destroy_file_t(f)
            f = nxt
        return out

    def folder(self, name: str) -> int:
        """Object id of GARMIN/<name>."""
        garmin = self.ls(self.ROOT).get("GARMIN")
        found = self.ls(garmin).get(name) if garmin else None
        if not found:
            sys.exit(f"No GARMIN/{name} folder on the watch.")
        return found

    def send(self, folder: int, paths: list[Path]) -> list[str]:
        """Copy files into a folder; returns the names that failed."""
        failed = []
        for path in paths:
            f = self.F(parent_id=folder, storage_id=self.storage, filename=path.name.encode(),
                       filesize=path.stat().st_size, filetype=44)  # 44 = LIBMTP_FILETYPE_UNKNOWN
            if self.lib.LIBMTP_Send_File_From_File(self.dev, str(path).encode(), self.C.byref(f), None, None):
                failed.append(path.name)
        return failed

    def delete(self, ids: list[int]) -> int:
        """Delete objects found in this session; returns how many went."""
        return sum(self.lib.LIBMTP_Delete_Object(self.dev, oid) == 0 for oid in ids)


# What the watch names a workout this script pushed: 'MM-DD-YY_<name>_workout.fit', with _01, _02... on re-import.
# Pushes before the year was added are 'MM-DD_...'. Garmin's own workouts don't start with a date.
PLANNED = re.compile(r"(\d\d)-(\d\d)(?:-(\d\d))?_.*_workout(?:_\d+)?\.fit$", re.I)
PENDING = re.compile(r"\d{4}-\d\d-\d\d\.fit")  # our files still waiting in NewFiles


def planned_workouts(on_watch: dict[str, int], today: date) -> dict[int, date]:
    """{filename: object_id} from GARMIN/Workouts -> {object_id: day} for the workouts this script put there."""
    out = {}
    for name, oid in on_watch.items():
        m = PLANNED.match(name)
        if m:
            try:
                out[oid] = date(2000 + int(m[3]) if m[3] else today.year, int(m[1]), int(m[2]))
            except ValueError:  # 02-29 without a year
                pass
    return out


def wipe():
    """Remove every workout this script put on the watch, including ones still waiting in NewFiles. Garmin's own stay."""
    if not check_watch():
        sys.exit("No Garmin watch found over USB. Plug it in and unlock it.")
    with Watch() as w:
        ids = list(planned_workouts(w.ls(w.folder("Workouts")), date.today()))
        ids += [oid for name, oid in w.ls(w.folder("NewFiles")).items() if PENDING.fullmatch(name)]
        print(f"{w.delete(ids)} of {len(ids)} planned workouts removed from the watch.")


def workouts():
    """Turn the latest coach plan into workout files and copy them onto the watch (Training > Workouts)."""
    if not (DATA / "coach.md").exists():
        sys.exit(f"No coach review for '{DATA.name}' yet. Run: garmin.py {DATA.name} coach")
    try:
        plan = plan_workouts((DATA / "coach.md").read_text())
    except ValueError as e:
        sys.exit(f"Can't turn the latest review into workouts ({e}). Ask for a new review and push again.")
    plan = [w for w in plan if w["date"] >= f"{datetime.now():%Y-%m-%d}"]
    if not plan:
        sys.exit("The latest review has no sessions from today on. Ask for a new review.")
    if not check_watch():
        sys.exit("No Garmin watch found over USB. Plug it in and unlock it.")
    out = DATA / "workouts"
    out.mkdir(exist_ok=True)
    (out / "plan.json").write_text(json.dumps(plan, indent=1))
    files = []
    for w in plan:  # names start with the date, so the watch list sorts and shows which day
        files.append(out / f"{w['date']}.fit")
        files[-1].write_bytes(workout_fit(w))
        print(w["name"])
    with Watch() as watch:
        missing = watch.send(watch.folder("NewFiles"), files)  # the watch imports anything in NewFiles when it is unplugged
        if missing:
            sys.exit(f"Not on the watch: {missing}. Files are in {out}; copy them to GARMIN/NewFiles by hand.")
        # Only once the new ones are safely in NewFiles: drop past days, and old versions of days the new plan replaces.
        today, new_days = date.today(), {date.fromisoformat(w["date"]) for w in plan}
        old = [oid for oid, d in planned_workouts(watch.ls(watch.folder("Workouts")), today).items() if d < today or d in new_days]
        removed = watch.delete(old)
    print(f"{len(files)} workouts copied, {removed} old ones removed. Unplug the watch to import them, then find them under Training > Workouts.")


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
    assert folder_files(tree, "Activity") == {"2026-05-07-08-11-04.fit": "31"}
    on_watch = {"01-04-27_Run_3x5_workout_01.fit": 1, "01-07-27_Run_3x6_workout.fit": 2, "12-30-26_Easy_workout.fit": 3,
                "11-14_Run_3x5_workout.fit": 4, "Sun_Salutations_workout.fit": 5, "COHRNCE.FIT": 6}
    assert planned_workouts(on_watch, date(2026, 11, 12)) == {1: date(2027, 1, 4), 2: date(2027, 1, 7), 3: date(2026, 12, 30), 4: date(2026, 11, 14)}
    usb = '+-o Hub@1  <class IOUSBHostDevice>\n  |   "idVendor" = 1452\n  |   "USB Serial Number" = "hub1"\n+-o Watch@2  <class IOUSBHostDevice>\n  |   "USB Serial Number" = "0000aa"\n  |   "idVendor" = 2334\n'
    assert watch_serial(usb) == "0000aa" and watch_serial(usb.replace("2334", "99")) is None
    assert add_pace({"total_distance": 5000, "total_timer_time": 1500})["pace_min_per_km"] == 5.0
    # 5 min running in zone 2 at 2.5 m/s, 1 min walking, 3 min running
    t0 = datetime(2026, 1, 1)
    rec = lambda i, cad: {"timestamp": t0 + timedelta(seconds=i), "cadence": cad, "enhanced_speed": 2.5, "heart_rate": 140}
    recs = [rec(i, 80) for i in range(300)] + [rec(i, 55) for i in range(300, 360)] + [rec(i, 80) for i in range(360, 541)]
    m = run_metrics(recs, [110, 130, 150, 170, 190])
    w = {"name": "test", "steps": [{"min": 5, "kind": "warmup"}, {"repeat": 3, "steps": [{"min": 5, "hr": [140, 150]}, {"min": 2, "kind": "recovery"}]}, {"kind": "cooldown"}]}
    from garmin_fit_sdk import Decoder, Stream
    d, err = Decoder(Stream.from_byte_array(bytearray(workout_fit(w)))).read()
    st = d["workout_step_mesgs"]
    assert not err and d["file_id_mesgs"][0]["type"] == "workout" and d["workout_mesgs"][0]["num_valid_steps"] == 5, (err, d)
    assert st[1]["custom_target_heart_rate_low"] == 240 and st[1]["duration_time"] == 300, st[1]
    assert st[3]["duration_type"] == "repeat_until_steps_cmplt" and st[3]["duration_step"] == 1 and st[3]["repeat_steps"] == 3, st[3]
    assert st[4]["duration_type"] == "open" and st[4]["intensity"] == "cooldown"
    ids = {Decoder(Stream.from_byte_array(bytearray(workout_fit({**w, "name": n})))).read()[0]["file_id_mesgs"][0]["serial_number"] for n in ("a", "b")}
    assert len(ids) == 2, "workouts written together must not share a file_id"
    review = """**The next two weeks.**

| Date | Session | Watch | How to run it | What it is for |
|---|---|---|---|---|
| Fri 2026-11-13 | Rest | | | |
| Sat 2026-11-14 | Run 3x5 | `5 walk, 3x(5 run <155, 2 walk), 4 walk` | Aim for 140 to 150 bpm | Longer runs |
| Sun 2026-11-15 | Easy RW 30 | 30 run 120-132 | Walk when it beeps | Easy volume |

| other | table |"""
    plan = plan_workouts(review)
    assert [(w["date"], w["name"]) for w in plan] == [("2026-11-14", "11-14-26 Run 3x5"), ("2026-11-15", "11-15-26 Easy RW 30")], plan
    assert plan[0]["steps"] == [{"min": 5, "kind": "warmup"}, {"repeat": 3, "steps": [{"min": 5, "kind": "active", "hr": [90, 155]}, {"min": 2, "kind": "recovery"}]},
                                {"min": 4, "kind": "cooldown"}], plan[0]
    assert plan[1]["steps"] == [{"min": 30, "kind": "active", "hr": [120, 132]}]
    for bad in ("5 jog", "3x(5 run", "5 run), 2 walk", "", "5 run 120"):
        try:
            watch_steps(bad)
            raise AssertionError(bad)
        except ValueError:
            pass
    assert m["longest_run_min"] == 5.0 and m["walk_pct"] == 11.1 and m["z2_pace_min_per_km"] == 6.67 and m["hr_zone_2_s"] == 540 and "z3_pace_min_per_km" not in m, m


if __name__ == "__main__":
    demo()
    if sys.argv[1:] == ["serve"]:
        serve()
    set_profile(sys.argv[1] if len(sys.argv) > 1 else "")
    cmd = sys.argv[2] if len(sys.argv) > 2 else "all"
    if cmd == "coach":
        coach(" ".join(sys.argv[3:]))  # garmin.py robin coach "optional note, e.g. sore calf"
    if cmd == "workouts":
        workouts()
    if cmd == "wipe":
        wipe()
    if cmd in ("pull", "all"):
        pull()
    if cmd in ("parse", "all"):
        parse()
