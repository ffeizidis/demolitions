"""Αποθήκευση των run (μεταδεδομένα + xlsx + PDF) — τοπικά ή σε Cloudflare R2.

Το `pipeline.run_pipeline` γράφει πάντα σε έναν τοπικό φάκελο (staging). Το
backend αναλαμβάνει τη μονιμότητα:

- `LocalStorage` — ο staging φάκελος *είναι* ο μόνιμος (runs/<id>/), όπως πριν.
- `R2Storage` — ανεβάζει τα αρχεία ως αντικείμενα `runs/<id>/<relpath>` σε bucket
  S3-συμβατό (Cloudflare R2). Ο δίσκος του host είναι εφήμερος, οπότε η μνήμη
  του ιστορικού ζει στο R2.

Επιλογή με env: `DEMOLITIONS_STORAGE=local|r2`. Όριο cache για PDF:
`DEMOLITIONS_PDF_CACHE_LIMIT` (bytes, default 1 GB) — όταν το συνολικό μέγεθος
των PDF ξεπεράσει το όριο, σβήνονται τα PDF των παλαιότερων run (τα
μεταδεδομένα μένουν· το zip τους ξαναφτιάχνεται κατ' απαίτηση από τη Διαύγεια).
"""

import json
import os
import shutil
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import quote

CONTENT_TYPES = {
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".json": "application/json; charset=utf-8",
    ".pdf": "application/pdf",
    ".zip": "application/zip",
}
DEFAULT_PDF_CAP = 2_000_000_000  # 2 GB


def content_type(name):
    return CONTENT_TYPES.get(Path(name).suffix.lower(), "application/octet-stream")


def _sum_usage(sizes):
    """Άθροισμα του sizes_by_run -> {storage_bytes, pdf_bytes}."""
    return {"storage_bytes": sum(s["total_bytes"] for s in sizes.values()),
            "pdf_bytes": sum(s["pdf_bytes"] for s in sizes.values())}


def _manifest_sort_key(manifest):
    """Νεότερο -> παλαιότερο, με βάση αμετάβλητο χρόνο δημιουργίας run."""
    if manifest.get("created_at"):
        return (3, manifest["created_at"], manifest.get("run_id", ""))
    if manifest.get("created"):
        return (2, manifest["created"], manifest.get("run_id", ""))
    return (1, manifest.get("_mtime", 0), manifest.get("run_id", ""))


def make_storage():
    """Backend από τα env vars (default: τοπικό runs/ δίπλα στο webui)."""
    kind = os.environ.get("DEMOLITIONS_STORAGE", "local").lower()
    cap = int(os.environ.get("DEMOLITIONS_PDF_CACHE_LIMIT", DEFAULT_PDF_CAP))
    if kind == "r2":
        return R2Storage(
            bucket=os.environ["R2_BUCKET"],
            account_id=os.environ["R2_ACCOUNT_ID"],
            access_key=os.environ["R2_ACCESS_KEY_ID"],
            secret_key=os.environ["R2_SECRET_ACCESS_KEY"],
            endpoint_url=os.environ.get("R2_ENDPOINT"),
            pdf_cap=cap,
        )
    runs_dir = Path(os.environ.get("DEMOLITIONS_RUNS_DIR",
                                   Path(__file__).parent.parent / "runs"))
    return LocalStorage(runs_dir, pdf_cap=cap)


# --------------------------------------------------------------------------- #

class LocalStorage:
    """Μόνιμη αποθήκευση στον δίσκο (runs/<id>/). Συμπεριφορά όπως πριν."""

    kind = "local"

    def __init__(self, runs_dir, pdf_cap=DEFAULT_PDF_CAP):
        self.runs = Path(runs_dir)
        self.runs.mkdir(parents=True, exist_ok=True)
        self.pdf_cap = pdf_cap

    # staging == ο μόνιμος φάκελος
    def staging_dir(self, run_id):
        return self.runs / run_id

    def prepare_staging(self, run_id):
        return self.runs / run_id

    def save_run(self, run_id, progress=None):
        pass  # ήδη γραμμένο στη θέση του

    def save_meta(self, run_id):
        pass  # τα json/xlsx είναι ήδη στον δίσκο

    def upload_pdf_immediate(self, run_id, relpath, local_path):
        pass  # τοπικά τα PDF μένουν στη θέση τους — δεν τα σβήνουμε

    def free_local_pdfs(self, run_id):
        pass  # τοπικά τα PDF ΕΙΝΑΙ η αποθήκη — δεν τα σβήνουμε

    def cleanup(self, run_id):
        pass

    def pull_cache(self, name, cache_dir):
        return False   # τοπικά το cache_dir είναι ήδη μόνιμο

    def push_cache(self, name, cache_dir):
        return False

    def presigned_url(self, run_id, relpath, **_):
        return None   # τοπικά δεν υπάρχει presigning — ο caller σερβίρει το αρχείο

    def _dir(self, run_id):
        d = (self.runs / run_id).resolve()
        if not d.is_relative_to(self.runs.resolve()):
            raise KeyError(run_id)
        return d

    def exists(self, run_id):
        try:
            return (self._dir(run_id) / "run.json").exists()
        except KeyError:
            return False

    def read_manifest(self, run_id):
        return json.loads((self._dir(run_id) / "run.json").read_text("utf-8"))

    def write_manifest(self, run_id, manifest):
        (self._dir(run_id) / "run.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=1), "utf-8")

    def open_member(self, run_id, relpath):
        """(iterable bytes, size) ή None — με προστασία traversal."""
        base = self._dir(run_id)
        p = (base / relpath).resolve()
        if not (p.is_relative_to(base) and p.is_file()):
            return None
        return _file_chunks(p), p.stat().st_size

    def list_runs(self):
        out = []
        for mf in self.runs.glob("*/run.json"):
            m = json.loads(mf.read_text("utf-8"))
            m["_mtime"] = mf.stat().st_mtime
            out.append(m)
        out.sort(key=_manifest_sort_key, reverse=True)
        return out

    def iter_pdfs(self, run_id):
        pdf_root = self._dir(run_id) / "pdf"
        if not pdf_root.is_dir():
            return
        for p in pdf_root.rglob("*.pdf"):
            yield str(p.relative_to(self._dir(run_id))), p.stat().st_size

    def delete_pdfs(self, run_id):
        shutil.rmtree(self._dir(run_id) / "pdf", ignore_errors=True)

    def delete_run(self, run_id):
        shutil.rmtree(self._dir(run_id), ignore_errors=True)

    def delete_orphans(self, keep_ids=(), log=lambda m: None):
        """Σβήνει φακέλους run χωρίς run.json (ημιτελή ανεβάσματα από run που
        σκοτώθηκαν πριν ολοκληρωθούν). `keep_ids`: εξαιρέσεις (π.χ. run που
        εκτελείται τώρα — το run.json γράφεται τελευταίο)."""
        keep = set(keep_ids)
        freed = 0
        for d in self.runs.iterdir():
            if (d.is_dir() and d.name not in keep
                    and not (d / "run.json").exists()):
                shutil.rmtree(d, ignore_errors=True)
                freed += 1
                log(f"Εκκαθάριση ημιτελούς (orphan) run: {d.name}")
        return freed

    def enforce_pdf_cap(self, log=lambda m: None):
        pass  # τοπικά δεν περιορίζουμε (ο δίσκος είναι ο δίσκος του χρήστη)

    def sizes_by_run(self):
        out = {}
        for d in self.runs.iterdir():
            if not (d.is_dir() and (d / "run.json").exists()):
                continue
            total = pdf = 0
            for p in d.rglob("*"):
                if p.is_file():
                    s = p.stat().st_size
                    total += s
                    if "pdf" in p.relative_to(d).parts:   # κάτω από pdf/
                        pdf += s
            out[d.name] = {"pdf_bytes": pdf, "total_bytes": total}
        return out

    def usage(self):
        return _sum_usage(self.sizes_by_run())


# --------------------------------------------------------------------------- #

class R2Storage:
    """Cloudflare R2 (S3-συμβατό) μέσω boto3. Κλειδιά: runs/<id>/<relpath>."""

    kind = "r2"

    def __init__(self, bucket, account_id, access_key, secret_key,
                 endpoint_url=None, pdf_cap=DEFAULT_PDF_CAP):
        import boto3  # lazy: μόνο το hosted χρειάζεται boto3
        from botocore.config import Config
        self.bucket = bucket
        self.pdf_cap = pdf_cap
        self.s3 = boto3.client(
            "s3",
            endpoint_url=endpoint_url
            or f"https://{account_id}.r2.cloudflarestorage.com",
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name="auto",
            config=Config(signature_version="s3v4"),
        )
        self._staging = Path(tempfile.gettempdir()) / "demolitions_staging"

    def _key(self, run_id, relpath):
        return f"runs/{run_id}/{relpath}"

    def staging_dir(self, run_id):
        d = self._staging / run_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def prepare_staging(self, run_id):
        """Κατεβάζει τα json (όχι τα PDF) σε staging για enrich_geocode."""
        d = self.staging_dir(run_id)
        for name in ("rows.json", "run.json"):
            self.s3.download_file(self.bucket, self._key(run_id, name),
                                  str(d / name))
        return d

    META_FILES = ("rows.json", "run.json")

    def _upload(self, run_id, rel):
        self.s3.upload_file(
            str(self.staging_dir(run_id) / rel), self.bucket,
            self._key(run_id, rel),
            ExtraArgs={"ContentType": content_type(rel)})

    def save_run(self, run_id, progress=None):
        """Ανεβάζει όλο τον staging φάκελο στο R2 (αρχικό ανέβασμα).

        Το run.json ανεβαίνει ΤΕΛΕΥΤΑΙΟ: ένα run "εμφανίζεται" στο ιστορικό
        (list_runs ψάχνει run.json) μόνο αφού έχουν ανέβει όλα τα υπόλοιπα,
        ώστε μια αποτυχία στη μέση να μη δείχνει ημιτελές run."""
        d = self.staging_dir(run_id)
        files = [str(p.relative_to(d)) for p in d.rglob("*") if p.is_file()]
        files.sort(key=lambda rel: rel == "run.json")   # run.json -> τελευταίο
        for i, rel in enumerate(files, 1):
            self._upload(run_id, rel)
            if progress:
                progress(i, len(files))

    def save_meta(self, run_id):
        """Ανεβάζει μόνο τα μικρά αρχεία (μετά τη γεωκωδικοποίηση) — όχι ξανά
        τα PDF (αποφυγή διπλού ανεβάσματος δεκάδων MB)."""
        d = self.staging_dir(run_id)
        for rel in self.META_FILES + (run_id + ".xlsx",):
            if (d / rel).is_file():
                self._upload(run_id, rel)

    def upload_pdf_immediate(self, run_id, relpath, local_path):
        """Ανεβάζει ένα PDF αμέσως μετά τη λήψη του και σβήνει το τοπικό
        αντίγραφο, ώστε να μην συσσωρεύονται GB PDF στον εφήμερο δίσκο κατά
        τη διάρκεια μεγάλων αναζητήσεων (π.χ. Αττική ~1500 άδειες)."""
        self.s3.upload_file(
            str(local_path), self.bucket, self._key(run_id, relpath),
            ExtraArgs={"ContentType": content_type(relpath)})
        try:
            Path(local_path).unlink()
        except OSError:
            pass

    def free_local_pdfs(self, run_id):
        """Σβήνει τα τοπικά PDF του staging μετά το ανέβασμα (τα PDF είναι
        ασφαλή στο R2)· μειώνει τη χρήση του εφήμερου δίσκου κατά τη
        γεωκωδικοποίηση."""
        shutil.rmtree(self.staging_dir(run_id) / "pdf", ignore_errors=True)

    def cleanup(self, run_id):
        shutil.rmtree(self._staging / run_id, ignore_errors=True)
        # αν το run απέτυχε πριν το save_run, καθαρίζει partial uploads στο R2
        # (προστασία από orphaned PDF objects όταν διακόπτεται η αναζήτηση)·
        # αν το exists() αποτύχει παροδικά, ΔΕΝ σβήνουμε τίποτα — τυχόν orphan
        # τα μαζεύει αργότερα το delete_orphans
        try:
            if not self.exists(run_id):
                self.delete_run(run_id)
        except Exception:
            pass

    def pull_cache(self, name, cache_dir):
        """Κατεβάζει κοινό μόνιμο cache αρχείο (π.χ. geocode.json) από το R2
        στον εφήμερο δίσκο πριν τη γεωκωδικοποίηση, ώστε η cache να επιβιώνει
        spin-down (το /tmp σβήνεται σε κάθε ύπνο). Κλειδί `cache/<name>`: έξω
        από το `runs/`, οπότε δεν μετριέται στη χρήση, δεν εκκαθαρίζεται από το
        cap, ούτε θεωρείται orphan. Επιστρέφει True αν υπήρχε στο R2."""
        dest = Path(cache_dir) / name
        try:
            obj = self.s3.get_object(Bucket=self.bucket, Key=f"cache/{name}")
        except Exception:
            return False
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(obj["Body"].read())
        return True

    def push_cache(self, name, cache_dir):
        """Ανεβάζει το cache αρχείο στο R2 μετά τη γεωκωδικοποίηση, ώστε οι νέες
        εγγραφές να επιβιώσουν τον επόμενο spin-down. True αν υπήρχε τοπικά."""
        src = Path(cache_dir) / name
        if not src.is_file():
            return False
        self.s3.upload_file(str(src), self.bucket, f"cache/{name}",
                            ExtraArgs={"ContentType": content_type(name)})
        return True

    def exists(self, run_id):
        """True/False μόνο όταν το R2 απαντά οριστικά. Παροδικά σφάλματα
        (timeout, throttling, DNS) ΑΝΑΔΙΔΟΝΤΑΙ: ένα ψευδές False εδώ έκανε το
        cleanup() να σβήνει ολόκληρο ένα ολοκληρωμένο run."""
        from botocore.exceptions import ClientError
        try:
            self.s3.head_object(Bucket=self.bucket,
                                Key=self._key(run_id, "run.json"))
            return True
        except ClientError as e:
            code = str(e.response.get("Error", {}).get("Code", ""))
            if code in ("404", "NoSuchKey", "NotFound"):
                return False
            raise

    def read_manifest(self, run_id):
        obj = self.s3.get_object(Bucket=self.bucket,
                                 Key=self._key(run_id, "run.json"))
        return json.loads(obj["Body"].read())

    def write_manifest(self, run_id, manifest):
        self.s3.put_object(
            Bucket=self.bucket, Key=self._key(run_id, "run.json"),
            Body=json.dumps(manifest, ensure_ascii=False, indent=1).encode("utf-8"),
            ContentType=content_type("run.json"))

    def open_member(self, run_id, relpath):
        key = self._key(run_id, relpath)
        try:
            obj = self.s3.get_object(Bucket=self.bucket, Key=key)
        except Exception:
            return None
        size = obj["ContentLength"]
        return self._resilient_body(key, obj["Body"], size), size

    def presigned_url(self, run_id, relpath, *, expires=21600, download_name=None):
        """Προσωρινό (default 6h) URL για απευθείας download του αντικειμένου
        από το R2, ώστε τα μεγάλα downloads να παρακάμπτουν τον (με όριο κίνησης)
        host. `download_name`: επιβάλλει filename στο browser μέσω
        Content-Disposition (RFC-5987 για ελληνικά ονόματα)."""
        params = {"Bucket": self.bucket, "Key": self._key(run_id, relpath)}
        if download_name:
            params["ResponseContentDisposition"] = (
                "attachment; filename*=UTF-8''" + quote(download_name, safe=""))
        return self.s3.generate_presigned_url(
            "get_object", Params=params, ExpiresIn=expires)

    def _resilient_body(self, key, body, size, chunk=65536):
        """Streamάρει ένα αντικείμενο· αν η σύνδεση με το R2 κοπεί στη μέση
        (συχνό σε πολύλεπτα downloads μεγάλου zip) ξανανοίγει με Range από το
        σημείο που έμεινε, αντί να κόψει ολόκληρο το zip. Μετά από αρκετές
        αποτυχίες παρατά αυτό το μέλος (ημιτελές) χωρίς να ρίξει το stream."""
        pos = 0
        attempt = 0
        while True:
            try:
                b = body.read(chunk)
            except Exception:
                try:
                    body.close()
                except Exception:
                    pass
                attempt += 1
                if pos >= size or attempt > 5:
                    return
                try:
                    body = self.s3.get_object(
                        Bucket=self.bucket, Key=key,
                        Range=f"bytes={pos}-")["Body"]
                except Exception:
                    return
                continue
            if not b:
                body.close()
                return
            pos += len(b)
            yield b

    def list_runs(self):
        found = []
        paginator = self.s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix="runs/"):
            for o in page.get("Contents", []):
                parts = o["Key"].split("/")
                if len(parts) == 3 and parts[2] == "run.json":
                    found.append((parts[1], o["LastModified"].timestamp()))

        def load(item):
            run_id, mtime = item
            try:   # ένα run που σβήνεται ταυτόχρονα δεν ρίχνει όλη τη λίστα
                m = self.read_manifest(run_id)
            except Exception:
                return None
            m["_mtime"] = mtime
            return m

        # ένα GET ανά run.json — παράλληλα, αλλιώς το ιστορικό με ~100 run
        # χρειαζόταν δευτερόλεπτα σειριακών round-trips (το boto3 client είναι
        # thread-safe)
        with ThreadPoolExecutor(max_workers=8) as pool:
            out = [m for m in pool.map(load, found) if m is not None]
        out.sort(key=_manifest_sort_key, reverse=True)
        return out

    def iter_pdfs(self, run_id):
        paginator = self.s3.get_paginator("list_objects_v2")
        prefix = self._key(run_id, "pdf/")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            for o in page.get("Contents", []):
                rel = o["Key"][len(f"runs/{run_id}/"):]
                yield rel, o["Size"]

    def _delete_keys(self, keys):
        for i in range(0, len(keys), 1000):
            self.s3.delete_objects(
                Bucket=self.bucket,
                Delete={"Objects": [{"Key": k} for k in keys[i:i + 1000]]})

    def delete_pdfs(self, run_id):
        keys = [self._key(run_id, rel) for rel, _ in self.iter_pdfs(run_id)]
        if keys:
            self._delete_keys(keys)

    def delete_run(self, run_id):
        paginator = self.s3.get_paginator("list_objects_v2")
        keys = []
        for page in paginator.paginate(Bucket=self.bucket,
                                       Prefix=f"runs/{run_id}/"):
            keys += [o["Key"] for o in page.get("Contents", [])]
        if keys:
            self._delete_keys(keys)

    def delete_orphans(self, keep_ids=(), log=lambda m: None):
        """Σβήνει prefixes runs/<id>/ με αντικείμενα αλλά ΧΩΡΙΣ run.json —
        ημιτελή ανεβάσματα από run που σκοτώθηκαν πριν το save_run (το
        run.json ανεβαίνει τελευταίο). Αόρατα στο ιστορικό, αλλά πιάνουν χώρο.
        `keep_ids`: run που ανεβαίνουν ακόμη (να μη θεωρηθούν orphan)."""
        keep = set(keep_ids)
        all_ids, have_manifest = set(), set()
        paginator = self.s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix="runs/"):
            for o in page.get("Contents", []):
                parts = o["Key"].split("/")     # runs / <rid> / …
                if len(parts) < 3:
                    continue
                all_ids.add(parts[1])
                if len(parts) == 3 and parts[2] == "run.json":
                    have_manifest.add(parts[1])
        freed = 0
        for rid in all_ids - have_manifest - keep:
            self.delete_run(rid)
            freed += 1
            log(f"Εκκαθάριση ημιτελούς (orphan) run: {rid}")
        return freed

    def enforce_pdf_cap(self, log=lambda m: None):
        sizes = self.sizes_by_run()              # ένα πέρασμα για τα μεγέθη
        total = 0
        kept_newest = False
        for m in self.list_runs():               # newest -> oldest
            if not m.get("has_pdfs"):
                continue
            run_id = m["run_id"]
            size = sizes.get(run_id, {}).get("pdf_bytes", 0)
            # has_pdfs=True αλλά μηδέν bytes στο R2 (σβήστηκαν out-of-band ή
            # crash μεταξύ delete_pdfs και rewrite) -> μόνιμη ασυνέπεια: το run
            # «χωρά» πάντα και κρατά την μπαγιάτικη σημαία για πάντα. Διόρθωση:
            # γράφε has_pdfs=False και μην το μετράς ούτε προσπαθήσεις delete.
            if size == 0:
                m.pop("_mtime", None)
                m["has_pdfs"] = False
                self.write_manifest(run_id, m)
                log(f"Διόρθωση μπαγιάτικου has_pdfs (χωρίς PDF στο R2): {run_id}")
                continue
            # το πιο πρόσφατο run κρατά πάντα τα PDF του (ο χρήστης μόλις
            # το έτρεξε)· τα υπόλοιπα μέχρι να γεμίσει το όριο
            if not kept_newest or total + size <= self.pdf_cap:
                total += size
                kept_newest = True
            else:
                self.delete_pdfs(run_id)
                m.pop("_mtime", None)
                m["has_pdfs"] = False
                self.write_manifest(run_id, m)
                log(f"Εκκαθάριση PDF παλαιότερου run: {run_id}")

    def usage(self):
        return _sum_usage(self.sizes_by_run())

    def sizes_by_run(self):
        out = {}
        paginator = self.s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix="runs/"):
            for o in page.get("Contents", []):
                parts = o["Key"].split("/")
                if len(parts) < 3:
                    continue
                e = out.setdefault(parts[1], {"pdf_bytes": 0, "total_bytes": 0})
                e["total_bytes"] += o["Size"]
                if "/pdf/" in o["Key"]:
                    e["pdf_bytes"] += o["Size"]
        return out


# --------------------------------------------------------------------------- #

def _file_chunks(path, chunk=65536):
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            yield b
