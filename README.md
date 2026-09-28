<img src="static/icon.png" alt="" width="72" align="right">

# demolitions (κατεδαφίσεις)

Εργαλείο έρευνας για τη χαρτογράφηση των αδειών κατεδάφισης στην Ελλάδα
(στο πνεύμα του [«Ο διαρκής θάνατος της μονοκατοικίας»](https://www.kathimerini.gr/investigations/563435530/o-diarkis-thanatos-tis-monokatoikias/)).

Αντλεί από τη **Διαύγεια** τις τελικές «Άδειες Κατεδάφισης» που δημοσιεύει το
ΤΕΕ μέσω του e-Άδειες, κατεβάζει το PDF κάθε άδειας, εξάγει τα στοιχεία
διεύθυνσης και το **περίγραμμα του κτίσματος** (συντεταγμένες ΕΓΣΑ87 από τη
φόρμα → WGS84· ~98% των αδειών), και παράγει spreadsheet + διαδραστικό
πίνακα/χάρτη. Όπου το PDF δεν δίνει συντεταγμένες, γεωκωδικοποιεί με
Nominatim/OpenStreetMap.

Αυτό το αρχείο είναι **τεχνικό** (εγκατάσταση, εκτέλεση, ανάπτυξη). Δείτε:

- **[ΟΔΗΓΟΣ.md](ΟΔΗΓΟΣ.md)** — οδηγίες χρήσης, μεθοδολογία και παραδοχές για
  μη τεχνικούς χρήστες (ερευνητές/δημοσιογράφους).
- **[REVIEW.md](REVIEW.md)** — προσανατολισμός για όποιον κάνει code review
  (μη προφανή δεδομένα, σχήματα αρχείων, invariants, κάλυψη tests).

## Εγκατάσταση

Χρειάζονται **Python 3** και το **pdftotext** (πακέτο poppler). Σε
Debian/Ubuntu/Mint:

```sh
sudo apt install python3-requests python3-openpyxl python3-flask poppler-utils
```

Εναλλακτικά (macOS, ή με virtualenv — στα σύγχρονα Linux το σκέτο
`pip install` εκτός venv δεν επιτρέπεται):

```sh
brew install poppler                     # macOS· σε Linux: sudo apt install poppler-utils
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt    # requests, openpyxl, flask, gunicorn, boto3, zipstream-ng
```

(Με venv, αντικαταστήστε το `python3` με `.venv/bin/python3` παρακάτω.)

## Εκτέλεση — Web UI

```sh
python3 webui.py
```

Ανοίγει μόνο του τον browser (τοπικά, 127.0.0.1· Ctrl-C για τερματισμό).
Για το πώς χρησιμοποιείται η σελίδα, βλ. [ΟΔΗΓΟΣ.md](ΟΔΗΓΟΣ.md).

## Εκτέλεση — γραμμή εντολών

```sh
python3 -m demolitions --area "Δήμος Δράμας" --from 01/01/2021 --to 31/12/2021 -o drama
python3 -m demolitions --area "Νομός Καβάλας" --from 01/01/2019 -o kavala
python3 -m demolitions --area "Περιφέρεια Κρήτης" --from 01/01/2024 -o kriti
python3 -m demolitions --area "Ελλάδα" --from 01/01/2023 --to 31/12/2023 --no-geocode -o ellada2023
```

Ημερομηνίες σε μορφή ΗΗ/ΜΜ/ΕΕΕΕ (δεκτή και η ΕΕΕΕ-ΜΜ-ΗΗ). `--area` δέχεται
δήμο, νομό/ΠΕ, περιφέρεια, «Ελλάδα», ή πολλά χωρισμένα με κόμμα. Σκέτο όνομα
(π.χ. «Καβάλας») = ο **δήμος** με αυτό το όνομα, αν υπάρχει· για τον παλιό νομό
γράψτε «Νομός Καβάλας», για τη στενότερη ΠΕ «ΠΕ Καβάλας». `--no-geocode` παραλείπει τις συντεταγμένες (πιο γρήγορο για μεγάλες
περιοχές). Κάθε run παράγει έναν φάκελο `<όνομα>/<όνομα>.xlsx` +
`pdf/<δήμος>/<έτος>/<ΑΔΑ>.pdf`.

## Δημόσια εγκατάσταση (δωρεάν: Render + Cloudflare R2)

Το app τρέχει σε container (χρειάζεται το `pdftotext`) και κρατά το ιστορικό
των run σε αποθήκη εκτός του (εφήμερου) δίσκου του host.

1. **Cloudflare R2** (δωρεάν 10 GB): φτιάξτε bucket + API token (Access Key +
   Secret)· σημειώστε το Account ID.
2. **Render**: New → Blueprint, δείξτε στο GitHub repo (διαβάζει το
   `render.yaml`). Στο dashboard ορίστε τα μυστικά `R2_BUCKET`,
   `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`. Πάρτε το
   **Deploy Hook URL**.
3. **GitHub**: repo → Settings → Secrets → Actions → `RENDER_DEPLOY_HOOK_URL`.
   Κάθε push στο `main` τρέχει τα tests και, αν περάσουν, αναπτύσσει αυτόματα
   (βλ. `.github/workflows/ci.yml`).

Στο hosted περιβάλλον τα PDF κρατιούνται στο R2 μόνο για τα **πιο πρόσφατα**
run· πάνω από το `DEMOLITIONS_PDF_CACHE_LIMIT` (default 1 GB) σβήνονται τα PDF
των παλαιότερων (τα μεταδεδομένα μένουν, το zip ξαναφτιάχνεται κατ' απαίτηση).
Η δωρεάν βαθμίδα του Render «κοιμάται» μετά από 15′ αδράνειας (~1′ στην πρώτη
επόμενη επίσκεψη).

### CORS στο R2 (για το «Λήψη όλων (zip)» / «Λήψη φιλτραρισμένων» στον browser)

Σε browser Chromium τα κουμπιά **«Λήψη όλων (zip)»** και **«Λήψη φιλτραρισμένων»**
κατεβάζουν τα PDF **απευθείας από το R2** (presigned URLs) και τα ζιπάρουν στον client,
παρακάμπτοντας το μετρημένο bandwidth του host. Για να πετύχει αυτό το
cross-origin `fetch()`, το R2 bucket πρέπει να επιτρέπει GET από το origin
του app. Χωρίς τον κανόνα, ο browser πέφτει πίσω στο πιο αργό server-side zip
(που χρεώνεται στο bandwidth του host).

Προσθέστε άπαξ τον εξής κανόνα CORS (Cloudflare dashboard: R2 → bucket →
Settings → CORS policy, ή `aws s3api put-bucket-cors`):

```json
[{"AllowedOrigins": ["https://demolitions.onrender.com"],
  "AllowedMethods": ["GET"], "AllowedHeaders": ["*"], "MaxAgeSeconds": 3600}]
```

Αντικαταστήστε το origin αν το deployed domain διαφέρει. Τα presigned URLs
χρησιμοποιούν τα υπάρχοντα διαπιστευτήρια R2 — δεν χρειάζεται έκθεση δημόσιου
(public) bucket.

Αφορά **μόνο** την R2-backed ανάπτυξη· η τοπική/CLI χρήση (`LocalStorage`) δεν
κάνει ποτέ presign και πάντα χρησιμοποιεί το server zip, οπότε τοπικά δεν
χρειάζεται CORS.

### Μεταβλητές περιβάλλοντος

| env | default | σημασία |
|---|---|---|
| `DEMOLITIONS_STORAGE` | `local` | `local` ή `r2` |
| `DEMOLITIONS_PDF_CACHE_LIMIT` | `1000000000` | όριο cache PDF (bytes) στο R2 |
| `DEMOLITIONS_CACHE_DIR` | `./cache` | φάκελος cache (λεξικό/geocode/PDF) |
| `DEMOLITIONS_RUNS_DIR` | `./runs` | φάκελος run (local backend) |
| `DEMOLITIONS_RATE_LIMIT_MAX_REQUESTS` | `12` | όριο αιτημάτων/παράθυρο ανά IP |
| `DEMOLITIONS_RATE_LIMIT_WINDOW_SECONDS` | `60` | παράθυρο rate limit (δευτ.) |
| `R2_BUCKET` / `R2_ACCOUNT_ID` / `R2_ACCESS_KEY_ID` / `R2_SECRET_ACCESS_KEY` | — | διαπιστευτήρια R2 |

## Αρχιτεκτονική

- **`demolitions/`** — ο πυρήνας: `diavgeia` (αναζήτηση + ταξινόμηση είδους),
  `pdfparse` (κατέβασμα/ανάλυση φόρμας· όροφοι/έκταση/περίγραμμα), `egsa87`
  (ΕΓΣΑ87→WGS84), `geocode` (Nominatim fallback), `areas` (Καλλικράτης),
  `greek`/`dimoi_data` (τονισμός), `output` (xlsx), `pipeline` (ενορχήστρωση με
  callbacks log/step/cancel), `storage` (backends).
- **`webui.py`** — Flask: σερβίρει τη σελίδα, τρέχει **ένα job τη φορά** σε
  background thread, polling στο `/api/status`. Κοινός πυρήνας με το CLI.
- **Ροή run (hosted):** αναζήτηση → ανάλυση PDF (staging σε εφήμερο δίσκο) →
  ανέβασμα στο R2 (τα τοπικά PDF ελευθερώνονται αμέσως) → εμφάνιση
  αποτελεσμάτων → γεωκωδικοποίηση (ανεβαίνουν ξανά μόνο τα json/xlsx) →
  εκκαθάριση cache PDF αν χρειαστεί.
- **Storage:** `LocalStorage` (`runs/<id>/`) ή `R2Storage` (αντικείμενα
  `runs/<id>/…`)· ίδιο interface, streaming serve/zip και από τα δύο.
- **Debug:** stdout/σφάλματα → Render **Logs**· η πρόοδος κάθε run στο UI και
  στο `/api/status`. Health check: `/healthz`.
- **Προσοχή (εφήμερος δίσκος):** πολύ μεγάλη περιοχή (νομός/χώρα) κατεβάζει
  εκατοντάδες PDF προσωρινά πριν το ανέβασμα — μπορεί να πιέσει τον δίσκο της
  δωρεάν βαθμίδας· για τέτοιες προτιμήστε τοπική εκτέλεση.

Περισσότερες λεπτομέρειες (σχήματα `run.json`/`rows.json`, invariants, μη
προφανή δεδομένα Διαύγειας/ΕΓΣΑ87) στο [REVIEW.md](REVIEW.md).

## Tests

```sh
python3 tests.py          # offline· «pip install moto[s3]» για το R2 test
```

## Cache

Όλα (αναζητήσεις, PDF, γεωκωδικοποιήσεις) αποθηκεύονται στο `cache/`· οι
επανεκτελέσεις είναι σχεδόν ακαριαίες. Σβήστε το `cache/search/` για να
ξαναφέρει πρόσφατα δημοσιευμένες πράξεις σε ήδη κατεβασμένα διαστήματα.
