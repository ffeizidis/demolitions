"""Η ροή ενός run, επαναχρησιμοποιήσιμη από CLI και web UI.

Δύο φάσεις: `run_pipeline` (αναζήτηση -> PDF -> xlsx, χωρίς συντεταγμένες)
και `enrich_geocode` (προσθέτει συντεταγμένες σε υπάρχον run και ξαναγράφει
το xlsx). Έτσι το spreadsheet είναι διαθέσιμο αμέσως και η αργή
γεωκωδικοποίηση (~1 αίτημα/δευτ.) τρέχει ως εμπλουτισμός.

Callbacks: `log(msg)` για κείμενο προόδου, `step(phase, i, n)` για μπάρα
προόδου (phases: search/pdf/geocode), `cancel()` -> bool για ακύρωση.
"""

import json
import shutil
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

from .areas import municipality_labels, normalize, resolve_area
from .diavgeia import (GREECE_TZ, KIND_KATEDAFISI, issue_date, permit_kind,
                       search_permits)
from .geocode import Geocoder, row_out_of_region
from .greek import pretty_area
from .output import write_xlsx
from .pdfparse import parse_decision

E_ADEIES_START = date(2018, 10, 1)


class CancelledRun(Exception):
    """Ο χρήστης ακύρωσε το run."""


class NoPermitsFound(Exception):
    """Καμία άδεια στο διάστημα/περιοχή."""


@dataclass
class RunResult:
    run_dir: Path
    xlsx_path: Path
    rows: list
    n_dups: int


def _check(cancel):
    if cancel and cancel():
        raise CancelledRun()


def _stage_pdf(ada, dimos, year, cache_dir, run_dir, pdf_root, pdf_callback,
               free_cache=False):
    """Αντιγράφει το PDF μιας άδειας στο run_dir και ειδοποιεί τον callback
    (που το ανεβάζει). Καλείται μέσα στη φάση download ώστε το ανέβασμα να
    επικαλύπτεται με το επόμενο κατέβασμα. Επιστρέφει το σχετικό path ή "".

    Με `free_cache=True` (hosted/R2) σβήνει και το αντίγραφο της cache μόλις
    αντιγραφεί στο staging — αλλιώς στον εφήμερο δίσκο του host συσσωρεύονται
    όλα τα PDF του run (η cache δεν επιβιώνει ούτως ή άλλως ένα spin-down).
    """
    src = Path(cache_dir) / "pdf" / f"{ada}.pdf"
    if not src.exists():
        return ""
    dest_dir = pdf_root / dimos / str(year)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{ada}.pdf"
    rel = str(dest.relative_to(run_dir))
    if not dest.exists():
        shutil.copy2(src, dest)
        if pdf_callback:
            pdf_callback(dest, rel)
    if free_cache:
        src.unlink(missing_ok=True)
    return rel


def _write_run_files(run_dir, rows, manifest):
    write_xlsx(rows, run_dir / (run_dir.name + ".xlsx"))
    (run_dir / "rows.json").write_text(
        json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    (run_dir / "run.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")


def _add_flag(row, flag):
    if flag and flag not in (row["flags"] or ""):
        row["flags"] = (row["flags"] + "; " if row["flags"] else "") + flag
        return True
    return False


def _flag_out_of_region(rows):
    """Σημαίνει εγγραφές γεωγραφικά εκτός της ζητούμενης περιοχής. Επιστρέφει
    το πλήθος που σημάνθηκαν.

    Έλεγχος ανά εγγραφή (row_out_of_region): σύγκριση των συντεταγμένων με το
    κέντρο της ΔΗΛΩΜΕΝΗΣ Περιφερειακής Ενότητας του ίδιου του record. Είναι
    άτρωτος στο ποσοστό «μόλυνσης» και στις ομωνυμίες, και — επειδή κρίνει κάθε
    εγγραφή μόνη της ως προς τη δική της ΠΕ — δεν παράγει ψευδώς θετικά σε
    γεωγραφικά ευρείες περιφέρειες (π.χ. απομακρυσμένα νησιά του Ν. Αιγαίου),
    σε αντίθεση με ένα bbox πάνω σε ολόκληρο το result set."""
    n_out = 0
    for row in rows:
        if _add_flag(row, row_out_of_region(row)):
            n_out += 1
    return n_out


def run_pipeline(area, from_date, to_date, out_dir, *, cache_dir,
                 log=print, step=None, cancel=None, pdf_callback=None,
                 free_cache=False):
    """Αναζήτηση + PDF + xlsx (χωρίς συντεταγμένες). Επιστρέφει RunResult."""
    if from_date < E_ADEIES_START:
        log(f"Προσοχή: το e-Άδειες ξεκίνησε τον 10/2018· πριν από "
            f"{E_ADEIES_START:%d/%m/%Y} δεν υπάρχουν ομοιόμορφα δεδομένα.")
        from_date = E_ADEIES_START
    # μελλοντικό «έως» = χιλιάδες άσκοπα (και μη cacheable) αιτήματα στη
    # Διαύγεια, ή OverflowError κοντά στο date.max
    today = datetime.now(GREECE_TZ).date()
    if to_date > today:
        to_date = today

    area_label, munis = resolve_area(area, cache_dir)
    muni_labels = municipality_labels(munis, cache_dir)
    area_label = pretty_area(area_label)
    log(f"Περιοχή: {area_label} ({len(munis)} δήμοι)")
    log(f"Διάστημα: {from_date:%d/%m/%Y} – {to_date:%d/%m/%Y}")

    def search_progress(msg):
        _check(cancel)
        log(msg)

    log("Αναζήτηση στη Διαύγεια…")
    if step:
        step("search", 0, 0)
    decisions = search_permits(from_date, to_date, munis, cache_dir,
                               progress=search_progress)
    log(f"Σύνολο: {len(decisions)} άδειες κατεδάφισης")
    if decisions:
        # μετρημένο μέσο ~3 MB/PDF στη Διαύγεια (παλιά εκτίμηση 300 KB ήταν ~10x χαμηλή)
        est_mb = max(1, round(len(decisions) * 3000 / 1024))
        log(f"Εκτιμώμενο μέγεθος PDF: ~{est_mb} MB")
    if not decisions:
        raise NoPermitsFound("Καμία άδεια στο διάστημα/περιοχή.")

    # κάθε run = ένας φάκελος με το spreadsheet και υποφάκελο pdf/<δήμος>/<έτος>/
    out = Path(out_dir)
    run_dir = out.with_suffix("") if out.suffix == ".xlsx" else out
    run_dir.mkdir(parents=True, exist_ok=True)
    pdf_root = run_dir / "pdf"

    log("Κατέβασμα και ανάλυση PDF…")
    rows = []
    seen_building = set()
    copied = 0
    for i, d in enumerate(decisions, 1):
        _check(cancel)
        if step:
            step("pdf", i, len(decisions))
        row = parse_decision(d, cache_dir)
        dt = issue_date(d)
        muni_code = d["extraFieldValues"]["municipality"]
        row["date"] = dt.isoformat()
        row["year"] = dt.year
        row["muni_code"] = muni_code
        row["dimos"] = muni_labels[muni_code]["display"]
        row["eidos"] = permit_kind(d.get("subject", "")) or KIND_KATEDAFISI
        flags = []
        # ίδιο κτίσμα με >1 τελικές άδειες (επανεκδόσεις) — συχνό φαινόμενο
        key = (muni_code,
               normalize(row["perigrafi"]), normalize(row["odos"]),
               row["ar_apo"])
        if row["perigrafi"] and key in seen_building:
            flags.append("πιθανό διπλό")
        seen_building.add(key)
        # τοιχίο/περίφραξη/πισίνα κ.λπ. — δεν είναι απώλεια κτίσματος
        if row.get("nonbuilding"):
            flags.append("μη κτίσμα")
        row["flags"] = "; ".join(flags)
        # το PDF αντιγράφεται/ανεβαίνει εδώ (όχι σε δεύτερο loop) ώστε το
        # ανέβασμα να τρέχει παράλληλα με το επόμενο download
        row["pdf_path"] = _stage_pdf(row["ada"], row["dimos"], row["year"],
                                     cache_dir, run_dir, pdf_root, pdf_callback,
                                     free_cache=free_cache)
        if row["pdf_path"]:
            copied += 1
        rows.append(row)
        if i % 25 == 0 or i == len(decisions):
            ok = sum(1 for r in rows if r["parse_ok"])
            log(f"  {i}/{len(decisions)} (επιτυχής ανάλυση: {ok})")
    del decisions   # ελευθερώνει μνήμη Διαύγειας πριν δημιουργηθεί το xlsx
    n_dups = sum(1 for r in rows if "πιθανό διπλό" in r["flags"])
    if n_dups:
        log(f"  Σημειώθηκαν {n_dups} πιθανά διπλά (ίδιος δήμος/διεύθυνση/περιγραφή).")
    log(f"PDF: {pdf_root}/ (αντιγράφηκαν {copied})")

    manifest = {
        "run_id": run_dir.name,
        "area": area_label,
        "area_query": area,
        "from": from_date.isoformat(),
        "to": to_date.isoformat(),
        "created": date.today().isoformat(),
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "n_rows": len(rows),
        "n_dups": n_dups,
        "geocoded": False,
        "has_pdfs": any(r["pdf_path"] for r in rows),
    }
    _write_run_files(run_dir, rows, manifest)
    xlsx_path = run_dir / (run_dir.name + ".xlsx")
    log(f"Γράφτηκε: {xlsx_path} ({len(rows)} γραμμές)")
    return RunResult(run_dir, xlsx_path, rows, n_dups)


def enrich_geocode(run_dir, *, cache_dir, log=print, step=None, cancel=None):
    """Συντεταγμένες σε υπάρχον run· ξαναγράφει xlsx/rows.json/run.json.

    Σε ακύρωση στη μέση, τα μερικά αποτελέσματα σώζονται και το run μένει
    geocoded=False ώστε να μπορεί να συνεχιστεί (η cache κάνει τα ήδη
    γεωκωδικοποιημένα σχεδόν ακαριαία).
    """
    run_dir = Path(run_dir)
    rows = json.loads((run_dir / "rows.json").read_text(encoding="utf-8"))
    manifest = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))

    log("Γεωκωδικοποίηση (Nominatim, ~1 αίτημα/δευτ. όταν δεν υπάρχει cache)…")
    geocoder = Geocoder(cache_dir)
    completed = False
    try:
        for i, row in enumerate(rows, 1):
            _check(cancel)
            if step:
                step("geocode", i, len(rows))
            if row.get("lat") is None:
                row["lat"], row["lon"], row["precision"] = \
                    geocoder.geocode_row(row, row["dimos"])
            # ο έλεγχος «εκτός περιοχής» (ομώνυμοι δήμοι κ.λπ.) γίνεται μετά
            # τον βρόχο — χωρίς κλήσεις Nominatim ανά δήμο (που καθυστερούσαν
            # δραματικά τη φάση αυτή)
            if i % 25 == 0 or i == len(rows):
                hit = sum(1 for r in rows[:i] if r.get("lat"))
                log(f"  {i}/{len(rows)} (με συντεταγμένες: {hit})")
        completed = True
        # δεύτερο πέρασμα: εντοπισμός εγγραφών γεωγραφικά εκτός της ζητούμενης
        # περιοχής (π.χ. άδειες Κρήτης σε αναζήτηση Αττικής λόγω ομώνυμου δήμου).
        # Χωρίς επιπλέον κλήσεις Nominatim.
        n_out = _flag_out_of_region(rows)
        if n_out:
            log(f"  {n_out} εγγραφές εκτός γεωγραφικών ορίων αναζήτησης.")
    finally:
        geocoder.close()
        manifest["geocoded"] = completed
        _write_run_files(run_dir, rows, manifest)
    log(f"Γράφτηκε: {run_dir / (run_dir.name + '.xlsx')} (με συντεταγμένες)")
