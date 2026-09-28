"""Γεωκωδικοποίηση διευθύνσεων με Nominatim (OpenStreetMap).

Κλιμακωτή ακρίβεια: οδός+αριθμός -> οδός -> οικισμός -> δήμος. Τα θετικά
αποτελέσματα και τα οριστικά «δεν βρέθηκε» κρατιούνται σε cache ώστε
επαναλήψεις να μην ξαναχτυπούν το API (όριο ~1 αίτημα/δευτερόλεπτο).
"""

import json
import math
import re
import time
from pathlib import Path

import requests

from .areas import normalize

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "demolitions-research/1.0 (ffeizidis@grnet.gr)"
# λογικά όρια Ελλάδας για απόρριψη άσχετων αποτελεσμάτων
LAT_RANGE = (34.5, 42.0)
LON_RANGE = (19.0, 30.1)

session = requests.Session()
session.headers["User-Agent"] = USER_AGENT

# ακρίβεια geocoding αρκετή για γεωγραφικό έλεγχο θέσης
_HIGH_PREC = frozenset(("κτίσμα (PDF)", "οδός+αριθμός", "οδός", "οικισμός"))

# Όριο απόστασης (km) σημείου από το κέντρο της δηλωμένης Περιφερειακής
# Ενότητας πάνω από το οποίο η εγγραφή θεωρείται «εκτός περιοχής». Επιλέχθηκε
# με βάση μετρήσεις στα πιο απομακρυσμένα ΝΟΜΙΜΑ σημεία της δικής τους ΠΕ:
# Αντικύθηρα -> ΠΕ Νήσων Αττικής ~191 km, Ορμένιο -> ΠΕ Έβρου ~173 km, Στρογγύλη
# -> ΠΕ Ρόδου ~161 km. Μια λάθος χρέωση σε άλλη περιφέρεια απέχει πολύ
# περισσότερο (Ηράκλειο Κρήτης χρεωμένο στον ομώνυμο δήμο Αττικής ~323 km). Τα
# 250 km αφήνουν άνετο περιθώριο και από τις δύο πλευρές (~59 km πάνω από το
# χειρότερο νόμιμο, ~73 km κάτω από το παράδειγμα λάθους).
PE_DISTANCE_KM = 250.0


def load_pe_centroids():
    """Κέντρα (lat, lon) ανά 2ψήφιο πρόθεμα κωδικού Καλλικράτη (Περιφερειακή
    Ενότητα). Πακεταρισμένο dataset — κανένα δίκτυο, ντετερμινιστικό για CI.
    Βλ. demolitions/data/pe_centroids.json."""
    path = Path(__file__).parent / "data" / "pe_centroids.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    return {k: tuple(v) for k, v in data["pe"].items()}


# φορτώνεται μία φορά στο import — μικρό λεξικό 75 εγγραφών
PE_CENTROIDS = load_pe_centroids()


def _haversine(la1, lo1, la2, lo2):
    la1, lo1, la2, lo2 = map(math.radians, (la1, lo1, la2, lo2))
    h = (math.sin((la2 - la1) / 2) ** 2
         + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2)
    return 6371 * 2 * math.asin(math.sqrt(h))


def _strip_dimos(label):
    """«Δήμος Δράμας»/«ΔΗΜΟΣ ΔΡΑΜΑΣ» -> «Δράμας»."""
    label = re.sub(r"^(ΔΗΜΟΣ|Δήμος)\s+", "", label)
    label = re.sub(r"\s*\([^)]+\)\s*$", "", label)
    return label.title()


def _poli_variants(poli, dimos):
    """Παραλλαγές του «Πόλη/Οικισμός» για τα queries.

    Το πεδίο συχνά γράφεται περιγραφικά («Οικισμός Ποταμιας Θάσου»,
    «ΔΡΑΜΑ/ΠΡΟΑΣΤΕΙΟ») και δεν ταιριάζει με το OSM· δοκιμάζουμε πρώτα
    καθαρισμένες μορφές (χωρίς «Οικισμός», χωρίς το όνομα του δήμου στο
    τέλος) — το σκέτο όνομα οικισμού είναι ό,τι ξέρει το OSM, ενώ η
    περιγραφική μορφή πιάνει εύκολα λάθος αποτέλεσμα (π.χ. δρόμο).
    """
    out = []
    for part in [poli] + poli.split("/"):
        part = part.strip()
        if not part:
            continue
        variants = [part]
        words = part.split()
        if words and normalize(words[0]) in ("ΟΙΚΙΣΜΟΣ", "ΣΥΝΟΙΚΙΣΜΟΣ",
                                             "ΟΙΚ.", "ΠΕΡΙΟΧΗ", "ΘΕΣΗ"):
            words = words[1:]
            variants.append(" ".join(words))
        if len(words) > 1 and normalize(words[-1]) == normalize(dimos):
            variants.append(" ".join(words[:-1]))
        out.extend(v for v in reversed(variants) if v and v not in out)
    return out


class Geocoder:
    def __init__(self, cache_dir):
        self.cache_path = Path(cache_dir) / "geocode.json"
        self.cache = {}
        if self.cache_path.exists():
            raw = json.loads(self.cache_path.read_text(encoding="utf-8"))
            # παλιό format: q -> [lat, lon] / None. Τα legacy None μπορεί να
            # κρύβουν παροδικές αστοχίες, οπότε τα ξαναδοκιμάζουμε.
            for q, value in raw.items():
                if isinstance(value, list):
                    self.cache[q] = {"status": "hit", "result": value}
                elif isinstance(value, dict) and value.get("status") in ("hit", "miss"):
                    self.cache[q] = value
        self._dirty = 0

    def _save(self):
        self.cache_path.write_text(
            json.dumps(self.cache, ensure_ascii=False), encoding="utf-8"
        )
        self._dirty = 0

    def _cache_get(self, q):
        cached = self.cache.get(q)
        if not cached:
            return None, False
        return cached.get("result"), True

    def _cache_put(self, q, result, *, status):
        self.cache[q] = {"status": status, "result": result}
        self._dirty += 1
        if self._dirty >= 20:
            self._save()

    def _query(self, q):
        cached, found = self._cache_get(q)
        if found:
            return cached
        for attempt in range(4):
            try:
                r = session.get(
                    NOMINATIM_URL,
                    params={"q": q, "format": "jsonv2", "limit": 1,
                            "countrycodes": "gr"},
                    timeout=30,
                )
            except requests.RequestException:
                if attempt == 3:
                    return None
                time.sleep(2 ** attempt)
                continue
            time.sleep(1.1)
            if r.ok:
                try:
                    hits = r.json()
                except ValueError:
                    # 200 με σελίδα σφάλματος (όχι JSON): παροδικό — ούτε cache
                    # «miss», ούτε να ρίξει όλη τη γεωκωδικοποίηση
                    if attempt == 3:
                        return None
                    time.sleep(2 ** attempt)
                    continue
                if hits:
                    lat, lon = float(hits[0]["lat"]), float(hits[0]["lon"])
                    if LAT_RANGE[0] <= lat <= LAT_RANGE[1] and \
                       LON_RANGE[0] <= lon <= LON_RANGE[1]:
                        result = [lat, lon]
                        self._cache_put(q, result, status="hit")
                        return result
                self._cache_put(q, None, status="miss")
                return None
            if r.status_code in (429, 500, 502, 503, 504):
                if attempt == 3:
                    return None
                time.sleep(2 ** attempt)
                continue
            return None
        return None

    def geocode_row(self, row, dimos_label):
        """Επιστρέφει (lat, lon, precision) ή (None, None, '')."""
        # «Δήμος Δράμας» -> «Δράμας» για πιο φυσικά queries
        dimos = row.get("dimos_pdf") or _strip_dimos(dimos_label)
        odos, ar, poli = row.get("odos"), row.get("ar_apo"), row.get("poli")
        # «Ο.Τ. 53» στο πεδίο οδού = οικοδομικό τετράγωνο, όχι οδός·
        # δεν υπάρχει στο OSM — πέφτουμε κατευθείαν στον οικισμό
        if odos and re.search(r"(^|\s)Ο\.?\s*Τ\.?(\s|\d|$)|ΟΙΚΟΔΟΜΙΚ",
                              normalize(odos)):
            odos = ""
        polis = _poli_variants(poli, dimos)

        tiers = []
        for p in polis:
            if odos and ar:
                tiers.append((f"{odos} {ar}, {p}, Ελλάδα", "οδός+αριθμός"))
        for p in polis:
            if odos:
                tiers.append((f"{odos}, {p}, Ελλάδα", "οδός"))
        if not polis and odos:
            # χωρίς «Πόλη/Οικισμός» αγκυρώνουμε την οδό στον δήμο — αλλιώς
            # οι tiers οδού δεν χτίζονταν καθόλου και η ακρίβεια κατέρρεε
            # αδικαιολόγητα σε «δήμος» παρότι υπάρχει διεύθυνση
            if ar:
                tiers.append((f"{odos} {ar}, Δήμος {dimos}, Ελλάδα",
                              "οδός+αριθμός"))
            tiers.append((f"{odos}, Δήμος {dimos}, Ελλάδα", "οδός"))
        for p in polis:
            tiers.append((f"{p}, Δήμος {dimos}, Ελλάδα", "οικισμός"))
            tiers.append((f"{p}, Ελλάδα", "οικισμός"))
        tiers.append((f"Δήμος {dimos}, Ελλάδα", "δήμος"))

        for q, precision in tiers:
            hit = self._query(q)
            if hit:
                return hit[0], hit[1], precision
        return None, None, ""

    def close(self):
        if self._dirty:
            self._save()


def row_out_of_region(row, *, distance_km=PE_DISTANCE_KM):
    """Flag «εκτός περιοχής αναζήτησης» με βάση τον ΚΩΔΙΚΟ της εγγραφής.

    Έλεγχος ανά εγγραφή, ανεξάρτητος από τις υπόλοιπες: συγκρίνει τις
    συντεταγμένες υψηλής ακρίβειας με το κέντρο της ΔΗΛΩΜΕΝΗΣ Περιφερειακής
    Ενότητας (2ψήφιο πρόθεμα του muni_code). Αν απέχουν > distance_km, η άδεια
    βρίσκεται μακριά από την περιοχή που δήλωσε ο αιτών — τυπική περίπτωση λάθος
    χρέωσης σε ομώνυμο δήμο (π.χ. κτίσμα Ηρακλείου Κρήτης χρεωμένο στον δήμο
    Ηρακλείου/Ν. Ηρακλείου Αττικής).

    Είναι άτρωτο στο ποσοστό «μόλυνσης» του result set (κρίνει κάθε εγγραφή
    μόνη της) και στην ομωνυμία του Nominatim (το άγκυρα είναι ο κωδικός/ΠΕ,
    όχι το αμφίσημο όνομα δήμου). Επιστρέφει string ή None."""
    if not row.get("lat") or row.get("precision") not in _HIGH_PREC:
        return None
    code = row.get("muni_code") or ""
    center = PE_CENTROIDS.get(code[:2])
    if not center:
        return None
    dist = _haversine(row["lat"], row["lon"], center[0], center[1])
    if dist <= distance_km:
        return None
    return f"~{dist:.0f}km εκτός περιοχής αναζήτησης"
