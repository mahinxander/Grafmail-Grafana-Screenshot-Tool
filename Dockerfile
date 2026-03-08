# =============================================================================
# GrafMail - Grafana Dashboard Screenshot Tool
# =============================================================================

FROM python:3.11-slim-bookworm

# Metadata
LABEL maintainer="Md Mahin Rahman"
LABEL description="Offline-ready GrafMail - Grafana dashboard screenshot capture and email tool"
LABEL version="1.0.0"

WORKDIR /app

# Non-interactive apt, unbuffered Python, no .pyc files
ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# Playwright browsers path (shared location accessible by non-root user)
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

# ── Step 1: System dependencies for Chromium ─────────────────────────────────
# These are the minimal libraries required by Playwright Chromium.
# Combined into a single RUN to minimise image layers.
RUN apt-get update && apt-get install -y --no-install-recommends \
    # Chromium runtime dependencies
    libnss3 \
    libnspr4 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libdbus-1-3 \
    libxkbcommon0 \
    libatspi2.0-0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libasound2 \
    libpango-1.0-0 \
    libcairo2 \
    libxshmfence1 \
    libx11-6 \
    libx11-xcb1 \
    libxcb1 \
    libxext6 \
    libxi6 \
    libxtst6 \
    # Networking / TLS (needed for health checks & SMTP)
    ca-certificates \
    curl \
    openssh-client \
    && rm -rf /var/lib/apt/lists/* /tmp/* /var/tmp/*

# ── Step 2: Python dependencies ──────────────────────────────────────────────
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# ── Step 3: Create non-root user and set up directories ──────────────────────
RUN useradd -m -s /bin/bash appuser && \
    mkdir -p /app/captures /app/logs /ms-playwright && \
    chown -R appuser:appuser /app /ms-playwright && \
    # Make captures/logs writable by any UID (safe: inside container only,
    # and these dirs are always bind-mounted over at runtime).
    chmod 777 /app/captures /app/logs && \
    # Pre-create .ssh directory for Paramiko known_hosts handling
    mkdir -p /home/appuser/.ssh && \
    chmod 700 /home/appuser/.ssh && \
    chown -R appuser:appuser /home/appuser/.ssh

# ── Step 4: Install ONLY Chromium as appuser ─────────────────────────────────
USER appuser
RUN playwright install chromium

# Verify Chromium works inside the container
RUN python -c "from playwright.sync_api import sync_playwright; print('Playwright Chromium verified')"

# ── Step 5: Application code ─────────────────────────────────────────────────
USER root
COPY --chown=appuser:appuser grafana_screenshot.py .
COPY --chown=appuser:appuser smtp_sender.py .

# Switch to non-root user for runtime
USER appuser

ENTRYPOINT ["python", "grafana_screenshot.py"]
CMD []
