# Instagram render worker: scripts/ig_render_gemini.py inside a container.
# Node (Remotion + Chrome Headless Shell) + python3 (orchestrator, stdlib-only)
# + ffmpeg (encode/dub). Built via deploy/render-worker/build.sh, which stages
# only the needed repo subset as the build context (see that script for the
# exact manifest).
#
# Runtime contract (Cloud Run Job):
#   env  GEMINI_API_KEY, IG_USER_ID, IG_LONG_TOKEN, IG_APP_SECRET (secrets),
#        S4L_GCS_BUCKET, AUTOPOSTER_API_BASE (default https://s4l.ai),
#        S4L_IDENTITY_JSON (optional pre-seeded installation identity)
#   args passed straight to ig_render_gemini.py, e.g.
#        --scenario tlh --force --upload-gcs --post trial --account matt_diak

FROM node:22-bookworm-slim

# Chrome Headless Shell shared libs (Remotion's documented Debian set),
# ffmpeg for encode/dub, python3 (3.11 on bookworm) for the orchestrator,
# fonts-liberation so the overlay font stack ("Helvetica Neue, Helvetica,
# Arial") resolves to Arial-metric-compatible Liberation Sans instead of
# DejaVu.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libnss3 libdbus-1-3 libatk1.0-0 libgbm1 libasound2 libxrandr2 \
    libxkbcommon0 libxfixes3 libxcomposite1 libxdamage1 \
    libatk-bridge2.0-0 libpango-1.0-0 libcairo2 libcups2 \
    ffmpeg python3 ca-certificates fonts-liberation \
  && rm -rf /var/lib/apt/lists/*

WORKDIR /app/mixer/remotion

# npm deps first (best layer caching), then the Remotion browser download.
COPY mixer/remotion/package.json mixer/remotion/package-lock.json ./
RUN npm ci --no-audit --no-fund
RUN npx remotion browser ensure

# Code + assets. public/ (328M clips) and audio/ (67M) are the fat layers.
COPY mixer/remotion/remotion.config.ts mixer/remotion/tsconfig.json ./
COPY mixer/remotion/scripts ./scripts
COPY mixer/remotion/src ./src
COPY mixer/remotion/public ./public
COPY mixer/audio /app/mixer/audio
COPY mixer/post_to_ig.py /app/mixer/post_to_ig.py
COPY scripts /app/scripts
COPY config.json /app/config.json
COPY deploy/render-worker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh && mkdir -p /app/mixer/remotion/out

ENV HOME=/root
ENTRYPOINT ["/entrypoint.sh"]
