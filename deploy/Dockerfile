# GmailCleaner — Cloud Run Job Container
#
# Build: docker build -t gmailcleaner -f deploy/Dockerfile .
# Run locally: docker run --rm -v $(pwd)/config:/app/config gmailcleaner scan

FROM python:3.11-slim

# System deps: ca-certificates needed for HTTPS unsubscribe calls.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first (layer cached unless requirements.txt changes).
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source — everything except secrets (never baked in).
COPY auth/       auth/
COPY gmail/      gmail/
COPY engines/    engines/
COPY store/      store/
COPY output/     output/
COPY config/settings.yaml config/settings.yaml
COPY main.py     .

# Create directories that the app writes to at runtime.
# On Cloud Run, /app/data will be replaced by a mounted Cloud Storage volume.
RUN mkdir -p data output logs

# Non-root user for security.
RUN useradd --create-home --shell /bin/bash appuser \
    && chown -R appuser:appuser /app
USER appuser

# Default: run a scan in dry-run mode.
# Cloud Scheduler overrides this with the desired command.
CMD ["python", "main.py", "scan", "--no-dry-run"]