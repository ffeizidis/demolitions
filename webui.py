#!/usr/bin/env python3
"""Web UI για το εργαλείο demolitions.

Τοπικά:  python3 webui.py   (ανοίγει μόνο του τον browser)
Hosted:  gunicorn -w 1 --threads 8 --timeout 0 -b 0.0.0.0:$PORT webui:app

Ένα run τη φορά, σε background thread· ο browser ρωτά την πρόοδο με polling
(/api/status). Τα run αποθηκεύονται μέσω του storage backend (τοπικός δίσκος
ή Cloudflare R2 — βλ. demolitions/storage.py).
"""

import io
import json
import os
import socket
import threading
import time
import traceback
import urllib.request
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from pathlib import Path
from urllib.parse import quote

from flask import Flask, Response, abort, jsonify, render_template, request

from zipstream import ZipStream

from demolitions.areas import AreaError, list_areas, normalize, resolve_area
from demolitions.diavgeia import GREECE_TZ, session as diavgeia_session
from demolitions.greek import pretty_area
from demolitions.output import write_xlsx
from demolitions.pipeline import (CancelledRun, E_ADEIES_START, NoPermitsFound,
                                  enrich_geocode, run_pipeline)
from demolitions.storage import content_type, make_storage

BASE = Path(__file__).parent
CACHE_DIR = Path(os.environ.get("DEMOLITIONS_CACHE_DIR", BASE / "cache"))
CACHE_DIR.mkdir(parents=True, exist_ok=True)
GITHUB_URL = "https://github.com/ffeizidis/demolitions"
START_TIME = time.time()
RATE_LIMIT_WINDOW_SECONDS = int(
    os.environ.get("DEMOLITIONS_RATE_LIMIT_WINDOW_SECONDS", "60")
)
RATE_LIMIT_MAX_REQUESTS = int(
    os.environ.get("DEMOLITIONS_RATE_LIMIT_MAX_REQUESTS", "12")
)
# Το Render free «κοιμίζει» το instance μετά από ~15' χωρίς εισερχόμενα
# αιτήματα — και θα σκότωνε το background thread στη μέση μιας μεγάλης
# αναζήτησης (η CPU δραστηριότητα ΔΕΝ μετράει, μόνο εισερχόμενα αιτήματα).
# Όσο τρέχει ένα job χτυπάμε μόνοι μας τη δημόσια διεύθυνση. Τοπικά (χωρίς
# RENDER_EXTERNAL_URL) δεν γίνεται τίποτα.
KEEPALIVE_URL = os.environ.get("RENDER_EXTERNAL_URL")

app = Flask(__name__)
store = make_storage()


class Job:
    def __init__(self):
        self.state = "idle"   # idle|running|geocoding|done|error|cancelled
        self.log = []
        self.phase = ""
        self.i = self.n = 0
        self.error = None
        self.result = None
        self.run_id = None
        self.meta = {}        # area/from/to για το ιστορικό όσο τρέχει
        self.cancel_event = threading.Event()

    def append(self, msg):
        self.log.append(msg)

    def step(self, phase, i, n):
        self.phase, self.i, self.n = phase, i, n


job = Job()
lock = threading.Lock()
rate_lock = threading.Lock()
rate_hits = {}


def _slug(label, from_date, to_date):
    name = normalize(label).lower().replace(" ", "-").replace(",", "")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{name}_{from_date}_{to_date}_{stamp}"


def _result_payload(run_id):
    manifest = store.read_manifest(run_id)
    manifest.pop("_mtime", None)
    manifest["xlsx_url"] = f"/runs/{run_id}/{run_id}.xlsx"
    manifest["zip_url"] = f"/zip/{run_id}.zip"
    return manifest


def _client_ip():
    # Πίσω από ΕΝΑΝ έμπιστο proxy (Render edge) μόνο το ΤΕΛΕΥΤΑΙΟ στοιχείο
    # του X-Forwarded-For είναι αξιόπιστο (το προσθέτει ο ίδιος ο proxy)· τα
    # αριστερότερα τα ελέγχει ο πελάτης — με το πρώτο, ένα τυχαίο XFF ανά
    # αίτημα θα έδινε φρέσκο rate-limit bucket κάθε φορά
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        ip = forwarded.rsplit(",", 1)[-1].strip()
        if ip:
            return ip
    return request.remote_addr or "unknown"


def _check_rate_limit(action):
    if RATE_LIMIT_MAX_REQUESTS <= 0 or RATE_LIMIT_WINDOW_SECONDS <= 0:
        return None
    now = time.time()
    cutoff = now - RATE_LIMIT_WINDOW_SECONDS
    key = (_client_ip(), action)
    with rate_lock:
        # καθάρισε εγγραφές που έληξαν εντελώς ώστε το dict να μη μεγαλώνει
        # απεριόριστα με IP που δεν ξαναεμφανίζονται
        for k in [k for k, ts in rate_hits.items() if not ts or ts[-1] <= cutoff]:
            if k != key:
                del rate_hits[k]
        hits = [t for t in rate_hits.get(key, []) if t > cutoff]
        if len(hits) >= RATE_LIMIT_MAX_REQUESTS:
            rate_hits[key] = hits
            retry_after = max(1, int(hits[0] + RATE_LIMIT_WINDOW_SECONDS - now))
            return retry_after
        hits.append(now)
        rate_hits[key] = hits
    return None


@app.before_request
def limit_expensive_routes():
    action = {
        "api_run": "run",
        "api_geocode": "geocode",
        "serve_zip": "zip",
        "api_zip_manifest": "zip",
        "api_xlsx_filtered": "zip",
        "serve_run_file": "zip",       # streamάρει μέσω του metered host
        # τα καταστροφικά DELETE δεν είναι αυθεντικοποιημένα — τουλάχιστον να
        # μην μπορούν να σφυροκοπούν τα delete-object του R2 χωρίς όριο
        "api_delete_all": "delete",
        "api_delete_all_pdfs": "delete",
        "api_delete_run": "delete",
        "api_delete_pdfs": "delete",
    }.get(request.endpoint)
    if not action:
        return None
    retry_after = _check_rate_limit(action)
    if retry_after is None:
        return None
    headers = {"Retry-After": str(retry_after)}
    if request.endpoint in ("serve_zip", "serve_run_file"):
        return Response("Rate limit exceeded.", status=429, headers=headers)
    return jsonify({
        "error": "Πάρα πολλά αιτήματα από την ίδια διεύθυνση. Δοκιμάστε ξανά σε λίγο."
    }), 429, headers


def _keepalive_loop(j):
    """Κρατά ξύπνιο το instance όσο τρέχει το job `j` (βλ. KEEPALIVE_URL)."""
    if not KEEPALIVE_URL:
        return
    url = KEEPALIVE_URL.rstrip("/") + "/healthz"
    while j.state in ("running", "geocoding"):
        try:
            urllib.request.urlopen(url, timeout=10).close()
        except Exception:
            pass
        for _ in range(50):          # ~4 λεπτά, αρκετά κάτω από το όριο των 15'
            if j.state not in ("running", "geocoding"):
                return
            time.sleep(5)


def _run_worker(j, area, from_date, to_date, run_id):
    try:
        staging = store.staging_dir(run_id)
        # κάθε PDF ανεβαίνει σε background ώστε το ανέβασμα (~0,1δ) να
        # επικαλύπτεται με το επόμενο κατέβασμα από τη Διαύγεια (~1δ). Λίγοι
        # workers αρκούν: τα PDF φτάνουν ένα-ένα, πιο αργά απ' ό,τι ανεβαίνουν.
        uploads = []
        pool = ThreadPoolExecutor(max_workers=4)

        def on_pdf(local_path, relpath):
            uploads.append(pool.submit(
                store.upload_pdf_immediate, run_id, relpath, local_path))

        try:
            run_pipeline(area, from_date, to_date, staging, cache_dir=CACHE_DIR,
                         log=j.append, step=j.step, cancel=j.cancel_event.is_set,
                         pdf_callback=on_pdf, free_cache=(store.kind == "r2"))
            for f in uploads:        # να ολοκληρωθούν ΟΛΑ πριν το save_run (που
                f.result()           # ανεβάζει το run.json τελευταίο) — αναδίδει
                                     # τυχόν σφάλμα ανεβάσματος ώστε το run να μη
                                     # «εμφανιστεί» με PDF που λείπουν
        finally:
            pool.shutdown(wait=True)
        j.append("Αποθήκευση αρχείων…")
        store.save_run(run_id, progress=lambda i, n: j.step("upload", i, n))
        store.free_local_pdfs(run_id)
        j.result = _result_payload(run_id)
        j.state = "geocoding"
        _geocode_worker(j, run_id, staging)
    except CancelledRun:
        j.append("Ακυρώθηκε.")
        j.state = "cancelled"
        store.cleanup(run_id)
    except (AreaError, NoPermitsFound) as e:
        j.error = str(e)
        j.state = "error"
        store.cleanup(run_id)
    except Exception as e:
        j.append(traceback.format_exc())
        j.error = f"Σφάλμα: {e}"
        j.state = "error"
        store.cleanup(run_id)


def _geocode_worker(j, run_id, staging=None):
    try:
        if staging is None:                       # κρύα εκκίνηση (παλιό run)
            staging = store.prepare_staging(run_id)
        # ζεστή geocode cache από το R2 (επιβιώνει spin-down)· οι ~2% εγγραφές
        # χωρίς συντεταγμένες PDF δεν ξαναχτυπούν το Nominatim κάθε run
        if store.pull_cache("geocode.json", CACHE_DIR):
            j.append("Φορτώθηκε η αποθηκευμένη geocode cache.")
        enrich_geocode(staging, cache_dir=CACHE_DIR, log=j.append,
                       step=j.step, cancel=j.cancel_event.is_set)
        store.save_meta(run_id)   # μόνο json/xlsx — τα PDF ανέβηκαν ήδη
        store.enforce_pdf_cap(log=j.append)
        # καθάρισε ημιτελή ανεβάσματα παλιότερων run που σκοτώθηκαν (το τωρινό
        # run έχει ήδη run.json, άρα δεν θεωρείται orphan — κρατιέται έτσι κι αλλιώς)
        store.delete_orphans(keep_ids=(run_id,), log=j.append)
        j.result = _result_payload(run_id)
        j.state = "done"
    except CancelledRun:
        store.save_meta(run_id)   # τα μερικά αποτελέσματα σώζονται (geocoded=false)
        j.append("Η γεωκωδικοποίηση ακυρώθηκε — οι μερικές συντεταγμένες σώθηκαν.")
        j.result = _result_payload(run_id)
        j.state = "done"
    except Exception as e:
        j.append(traceback.format_exc())
        j.error = f"Σφάλμα γεωκωδικοποίησης: {e}"
        j.state = "error"
    finally:
        # σώσε ό,τι έμαθε το Nominatim (και σε ακύρωση/σφάλμα — οι μερικές
        # εγγραφές είναι έγκυρες) πριν σβηστεί ο εφήμερος δίσκος
        store.push_cache("geocode.json", CACHE_DIR)
        store.cleanup(run_id)


def _start(target, *args, state="running", result=None, run_id=None, meta=None):
    """Ξεκινά νέο job αν δεν τρέχει ήδη κάποιο. Επιστρέφει True/False.

    Ο worker δουλεύει πάνω στο instance `j` που του περνιέται (όχι στο global
    `job`), ώστε ένα μεταγενέστερο run να μην μπορεί ποτέ να αλλοιώσει την
    κατάσταση ενός άλλου."""
    global job
    with lock:
        if job.state in ("running", "geocoding"):
            return False
        job = Job()
        job.state = state
        job.result = result
        job.run_id = run_id
        job.meta = meta or {}
        threading.Thread(target=target, args=(job,) + args, daemon=True).start()
        if KEEPALIVE_URL:
            threading.Thread(target=_keepalive_loop, args=(job,),
                             daemon=True).start()
        return True


@app.get("/healthz")
def healthz():
    return "ok", 200


@app.get("/favicon.ico")
def favicon():
    return app.send_static_file("favicon.ico")


@app.get("/")
def index():
    return render_template("index.html", today=date.today().isoformat(),
                           start=E_ADEIES_START.isoformat(),
                           r2=(store.kind == "r2"))


@app.get("/api/about")
def api_about():
    runs = store.list_runs()
    u = store.usage()
    return jsonify({
        "backend": store.kind,
        "n_runs": len(runs),
        "n_permits": sum(m.get("n_rows") or 0 for m in runs),
        "storage_bytes": u["storage_bytes"],
        "pdf_bytes": u["pdf_bytes"],
        "pdf_cap": store.pdf_cap,
        "uptime_seconds": int(time.time() - START_TIME),
        "version": (os.environ.get("RENDER_GIT_COMMIT") or "")[:7],
        "github": GITHUB_URL,
        "data_since": E_ADEIES_START.isoformat(),
    })


@app.get("/api/areas")
def api_areas():
    return jsonify(list_areas(CACHE_DIR))


@app.post("/api/run")
def api_run():
    data = request.get_json(force=True, silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Μη έγκυρο αίτημα."}), 400
    try:
        from_date = date.fromisoformat(data.get("from") or "")
        to_date = date.fromisoformat(data.get("to") or "")
    except (TypeError, ValueError):
        return jsonify({"error": "Μη έγκυρη ημερομηνία."}), 400
    # όχι μελλοντικό «έως» (βλ. run_pipeline) — κλειδώνεται κι εδώ ώστε το
    # run_id/ιστορικό να δείχνουν το πραγματικό διάστημα
    to_date = min(to_date, datetime.now(GREECE_TZ).date())
    if from_date > to_date:
        return jsonify({"error": "Η αρχή του διαστήματος είναι μετά το τέλος του."}), 400
    area = (data.get("area") or "").strip()
    try:
        label, _ = resolve_area(area, CACHE_DIR)
    except AreaError as e:
        return jsonify({"error": str(e)}), 400
    run_id = _slug(label, from_date, to_date)
    meta = {"area": pretty_area(label),
            "from": from_date.isoformat(), "to": to_date.isoformat()}
    if not _start(_run_worker, area, from_date, to_date, run_id,
                  run_id=run_id, meta=meta):
        return jsonify({"error": "Εκτελείται ήδη αναζήτηση."}), 409
    return jsonify({"ok": True, "run_id": run_id})


@app.post("/api/geocode/<run_id>")
def api_geocode(run_id):
    if not store.exists(run_id):
        abort(404)
    if not _start(_geocode_worker, run_id, state="geocoding",
                  result=_result_payload(run_id), run_id=run_id):
        return jsonify({"error": "Εκτελείται ήδη αναζήτηση."}), 409
    return jsonify({"ok": True})


@app.get("/api/status")
def api_status():
    since = max(0, request.args.get("since", 0, type=int))
    return jsonify({
        "state": job.state,
        "log": job.log[since:],
        "cursor": len(job.log),
        "phase": job.phase, "i": job.i, "n": job.n,
        "error": job.error,
        "result": job.result,
    })


@app.post("/api/cancel")
def api_cancel():
    with lock:
        j = job
    j.cancel_event.set()
    return jsonify({"ok": True})


@app.get("/api/runs")
def api_runs():
    runs = store.list_runs()
    sizes = store.sizes_by_run()
    for m in runs:
        m.pop("_mtime", None)
        s = sizes.get(m["run_id"], {})
        m["total_bytes"] = s.get("total_bytes", 0)
        m["pdf_bytes"] = s.get("pdf_bytes", 0)
    active_id = job.run_id if job.state in ("running", "geocoding") else None
    if active_id:
        for m in runs:
            if m["run_id"] == active_id:
                m["active"] = job.state
                break
        else:  # δεν έχει γραφτεί ακόμη run.json (φάση αναζήτησης/PDF)
            runs.insert(0, {"run_id": active_id, **job.meta, "n_rows": None,
                            "geocoded": False, "active": job.state,
                            "created": date.today().isoformat()})
        runs.sort(key=lambda m: 0 if m.get("active") else 1)
    return jsonify(runs)


@app.delete("/api/runs")
def api_delete_all():
    """Διαγράφει όλα τα run (εκτός από αυτό που τυχόν εκτελείται τώρα)."""
    with lock:
        active = job.run_id if job.state in ("running", "geocoding") else None
    deleted = 0
    for m in store.list_runs():
        if m["run_id"] == active:
            continue
        store.delete_run(m["run_id"])
        deleted += 1
    return jsonify({"ok": True, "deleted": deleted})


@app.delete("/api/pdfs")
def api_delete_all_pdfs():
    """Καθαρίζει τα PDF όλων των run (κρατά μεταδεδομένα/xlsx) — ελευθερώνει χώρο."""
    with lock:
        active = job.run_id if job.state in ("running", "geocoding") else None
    cleared = 0
    for m in store.list_runs():
        if m["run_id"] == active or not m.get("has_pdfs"):
            continue
        store.delete_pdfs(m["run_id"])
        m.pop("_mtime", None)
        m["has_pdfs"] = False
        store.write_manifest(m["run_id"], m)
        cleared += 1
    # ελευθέρωσε και τα ημιτελή ανεβάσματα (orphan) που δεν φαίνονται στο ιστορικό.
    # Υπό το lock και με ΦΡΕΣΚΟ active: ένα run που ξεκίνησε κατά τον βρόχο
    # παραπάνω έχει ήδη ανεβασμένα PDF αλλά όχι run.json — θα έμοιαζε orphan
    with lock:
        active = job.run_id if job.state in ("running", "geocoding") else None
        orphans = store.delete_orphans(keep_ids=(active,) if active else ())
    return jsonify({"ok": True, "cleared": cleared, "orphans": orphans})


@app.delete("/api/runs/<run_id>")
def api_delete_run(run_id):
    with lock:
        if job.state in ("running", "geocoding") and job.run_id == run_id:
            return jsonify({"error": "Το run εκτελείται αυτή τη στιγμή."}), 409
    if not store.exists(run_id):
        abort(404)
    store.delete_run(run_id)
    return jsonify({"ok": True})


@app.delete("/api/runs/<run_id>/pdfs")
def api_delete_pdfs(run_id):
    """Σβήνει μόνο τα PDF του run (κρατά μεταδεδομένα/xlsx) — ελευθερώνει χώρο."""
    with lock:
        if job.state in ("running", "geocoding") and job.run_id == run_id:
            return jsonify({"error": "Το run εκτελείται αυτή τη στιγμή."}), 409
    if not store.exists(run_id):
        abort(404)
    store.delete_pdfs(run_id)
    m = store.read_manifest(run_id)
    m.pop("_mtime", None)
    m["has_pdfs"] = False
    store.write_manifest(run_id, m)
    return jsonify({"ok": True})


@app.get("/api/runs/<run_id>/rows")
def api_rows(run_id):
    member = store.open_member(run_id, "rows.json")
    if not member:
        abort(404)
    gen, _ = member
    return Response(gen, mimetype="application/json")


@app.get("/runs/<run_id>/<path:filename>")
def serve_run_file(run_id, filename):
    member = store.open_member(run_id, filename)
    if not member:
        abort(404)
    gen, size = member
    headers = {"Content-Length": str(size)}
    if filename.endswith(".xlsx"):
        headers["Content-Disposition"] = _attachment(filename.rsplit("/", 1)[-1])
    return Response(gen, mimetype=content_type(filename), headers=headers)


def _attachment(name):
    """Content-Disposition με RFC 5987 (τα headers δέχονται μόνο latin-1)."""
    return "attachment; filename*=UTF-8''" + quote(name)


def _load_rows(run_id):
    """Φορτώνει το rows.json ενός run (λίστα dict) ή [] αν λείπει."""
    member = store.open_member(run_id, "rows.json")
    return json.loads(b"".join(member[0])) if member else []


def _filtered_xlsx_bytes(subset):
    """Φτιάχνει xlsx (bytes) για το ήδη φιλτραρισμένο υποσύνολο γραμμών ενός
    run (με τη σειρά του rows.json — ο καλών φιλτράρει χωρίς αναδιάταξη). Τα
    pivot φύλλα ξαναϋπολογίζονται για το υποσύνολο. Επιστρέφει None αν δεν
    υπάρχει καμία γραμμή."""
    if not subset:
        return None
    buf = io.BytesIO()
    write_xlsx(subset, buf)             # δέχεται file-like — όχι disk
    return buf.getvalue()


def _diavgeia_pdf(ada):
    """Streamάρει το PDF μιας πράξης από τη Διαύγεια (για zip χωρίς cache)."""
    url = f"https://diavgeia.gov.gr/doc/{ada}"
    try:
        with diavgeia_session.get(url, timeout=120, stream=True) as r:
            if not r.ok:
                return
            for chunk in r.iter_content(65536):
                yield chunk
    except Exception:
        return


def _zip_member(run_id, arc, ada=None):
    """Lazy: ανοίγει το αρχείο (στο R2: get_object) μόνο όταν φτάσει η σειρά
    του στο stream. Αλλιώς θα ανοίγαμε δεκάδες συνδέσεις/round-trips ΠΡΙΝ
    σταλεί το πρώτο byte — ο proxy κάνει timeout (502) και κρατάμε πολλές
    ανοιχτές συνδέσεις ταυτόχρονα.

    Αν το cached αντικείμενο λείπει στο R2 (μερικό ανέβασμα/εκκαθάριση/race)
    και ξέρουμε το ΑΔΑ, πέφτει πίσω σε λήψη από τη Διαύγεια — ώστε το zip να
    μη βγάζει 0-byte εγγραφή και να είναι μικρότερο από το αναμενόμενο."""
    m = store.open_member(run_id, arc)
    if m:
        yield from m[0]
    elif ada:
        yield from _diavgeia_pdf(ada)


def _ada_filter():
    """Φίλτρο ΑΔΑ για το φιλτραρισμένο zip. GET = πλήρες zip (None)· POST =
    ΠΑΝΤΑ φιλτραρισμένο. Η λίστα διαβάζεται από το σώμα — ένα `ada` form field
    ανά ΑΔΑ (`request.form.getlist`), ή εναλλακτικά JSON `{"ada":[...]}`. Κάθε
    ΑΔΑ ταξιδεύει ως ξεχωριστή τιμή, οπότε είναι ασφαλές ανεξαρτήτως
    περιεχομένου (π.χ. κόμμα) και χωρίς όριο μήκους URL — σε αντίθεση με ένα
    `?ada=a,b,c` query string, που το comma-split θα έσπαγε και που για ~1500
    γραμμές ξεπερνά τα όρια header proxy/CDN (414/400).

    Ένα POST χωρίς ΑΔΑ δίνει ΚΕΝΟ set (όχι None): αλλιώς ένα κενό φίλτρο θα
    κατέβαζε ολόκληρο το run ως «φιλτραρισμένο»."""
    if request.method != "POST":
        return None
    ada = request.form.getlist("ada")
    if not ada:
        body = request.get_json(silent=True) or {}
        raw = body.get("ada") if isinstance(body, dict) else None
        if isinstance(raw, list):
            ada = [a for a in raw if isinstance(a, str)]
    return {a for a in ada if a}


@app.route("/zip/<run_id>.zip", methods=["GET", "POST"])
def serve_zip(run_id):
    if not store.exists(run_id):
        abort(404)
    manifest = store.read_manifest(run_id)
    rows = _load_rows(run_id)
    ada_set = _ada_filter()
    if ada_set is not None:                         # φιλτραρισμένο zip
        rows = [r for r in rows if r.get("ada") in ada_set]
        if not rows:
            return jsonify({"error": "Καμία γραμμή δεν ταιριάζει με τα ΑΔΑ."}), 400

    zs = ZipStream()
    xlsx = f"{run_id}.xlsx"
    if ada_set is not None:
        # ξαναφτιάχνουμε το xlsx για το υποσύνολο (pivot ανά υποσύνολο)· είναι
        # μικρό (~KB) οπότε το ετοιμάζουμε αμέσως αντί lazy. Η σειρά των γραμμών
        # ακολουθεί το rows.json (όχι την τρέχουσα ταξινόμηση του πίνακα) — ίδια
        # συμπεριφορά με το πλήρες xlsx, σκόπιμα συνεπής. Τα `rows` είναι ήδη
        # το φιλτραρισμένο υποσύνολο — δεν ξαναδιαβάζουμε το rows.json.
        data = _filtered_xlsx_bytes(rows)
        if data is not None:
            zs.add(data=iter([data]), arcname=xlsx)
    else:
        zs.add(data=_zip_member(run_id, xlsx), arcname=xlsx)
    cached = manifest.get("has_pdfs")
    for r in rows:
        arc = r.get("pdf_path")
        if not arc:
            continue
        if cached:                                  # από την αποθήκη (lazy)·
            # με fallback σε Διαύγεια αν λείπει το αντικείμενο
            zs.add(data=_zip_member(run_id, arc, r.get("ada")), arcname=arc)
        elif r.get("ada"):                           # κατ' απαίτηση από Διαύγεια
            zs.add(data=_diavgeia_pdf(r["ada"]), arcname=arc)
    return Response(zs, mimetype="application/zip",
                    headers={"Content-Disposition": _attachment(f"{run_id}.zip")})


@app.get("/api/runs/<run_id>/zip-manifest")
def api_zip_manifest(run_id):
    """Δίνει στον browser presigned URL για κάθε αρχείο, ώστε να κατεβάσει
    και να φτιάξει το zip τοπικά — παρακάμπτοντας τον μετρημένο host (R2 μόνο).
    Στον τοπικό δίσκο (ή σε run χωρίς PDF στο R2) επιστρέφει το server-side zip."""
    if not store.exists(run_id):
        abort(404)
    server_url = f"/zip/{run_id}.zip"
    manifest = store.read_manifest(run_id)
    if store.kind == "r2" and manifest.get("has_pdfs"):
        rows = _load_rows(run_id)
        xlsx = f"{run_id}.xlsx"
        files = [{"name": xlsx,
                  "url": store.presigned_url(run_id, xlsx, download_name=xlsx)}]
        for r in rows:
            arc = r.get("pdf_path")
            # traversal guard — το _key δεν ελέγχει το relpath
            if arc and arc.startswith("pdf/"):
                files.append({"name": arc,
                              "url": store.presigned_url(run_id, arc)})
        return jsonify({"mode": "client", "zipname": f"{run_id}.zip",
                        "files": files, "fallback": server_url})
    return jsonify({"mode": "server", "url": server_url})


@app.post("/api/runs/<run_id>/xlsx-filtered")
def api_xlsx_filtered(run_id):
    """Επιστρέφει xlsx (κατέβασμα) μόνο για τις γραμμές των οποίων το ΑΔΑ
    στέλνεται στο σώμα `{"ada": [...]}` — για τη «Λήψη φιλτραρισμένων». POST
    επειδή η λίστα ΑΔΑ μπορεί να είναι μεγάλη (~1500). Το xlsx είναι μικρό, οπότε
    σερβίρεται μέσω του host (όχι presigned)."""
    if not store.exists(run_id):
        abort(404)
    data = request.get_json(force=True, silent=True) or {}
    ada = data.get("ada")
    if not isinstance(ada, list) or not all(isinstance(a, str) for a in ada):
        return jsonify({"error": "Το «ada» πρέπει να είναι λίστα από strings."}), 400
    ada_set = set(ada)
    body = _filtered_xlsx_bytes(
        [r for r in _load_rows(run_id) if r.get("ada") in ada_set])
    if body is None:                       # κανένα γνωστό ΑΔΑ -> τίποτα να σταλεί
        return jsonify({"error": "Καμία γραμμή δεν ταιριάζει με τα ΑΔΑ."}), 400
    name = f"{run_id}-φιλτραρισμένο.xlsx"
    return Response(body, mimetype=content_type(name),
                    headers={"Content-Disposition": _attachment(name)})


def main():
    port = None
    for p in range(8741, 8761):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", p))
                port = p
                break
            except OSError:
                continue
    url = f"http://127.0.0.1:{port}/"
    print(f"demolitions web UI: {url}  (Ctrl-C για τερματισμό)")
    threading.Timer(1.0, webbrowser.open, [url]).start()
    app.run(host="127.0.0.1", port=port, threaded=True, debug=False)


if __name__ == "__main__":
    main()
