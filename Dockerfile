FROM python:3.12-slim

# pdftotext (poppler) — απαραίτητο για την ανάλυση των PDF
RUN apt-get update \
    && apt-get install -y --no-install-recommends poppler-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# χωρίς root: ο κώδικας μένει read-only (ανήκει στον root)· γράφεται μόνο το /tmp
RUN useradd --system --create-home app
USER app

# cache (λεξικό/geocode) σε εφήμερο /tmp· τα run μένουν στο R2 (βλ. env)
ENV DEMOLITIONS_CACHE_DIR=/tmp/demolitions-cache
# (και τα run, αν τρέξει χωρίς R2 — το /app δεν είναι εγγράψιμο για τον app)
ENV DEMOLITIONS_RUNS_DIR=/tmp/demolitions-runs

# περιορίζει τα glibc malloc arenas ώστε να μη διογκώνεται το RSS από
# κατακερματισμό όταν πολλά threads κάνουν alloc/free των MB-μεγέθους PDF
ENV MALLOC_ARENA_MAX=2

# Render/hosted ορίζει $PORT· ένας worker (single-job model) + threads για το
# polling και το streaming zip· timeout 0 ώστε να μην κόβεται το long download.
# 4 threads (αντί 8): τρέχει πάντα ένα search τη φορά, ώστε 4 αρκούν για polling
# + λίγα zip downloads, μειώνοντας στο μισό το worst-case ταυτόχρονο RSS stacking
CMD ["sh", "-c", "gunicorn -w 1 --threads 4 --timeout 0 -b 0.0.0.0:${PORT:-8000} webui:app"]
