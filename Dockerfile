# Chromium baked into the build layer so 魔搭创空间 cold starts never re-download it.
FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

# playwright's node driver links libatomic.so.1, absent on slim-bookworm; the CLI
# must load it before `install --with-deps` can run, so apt-install it first.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libatomic1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && playwright install --with-deps chromium

COPY . ./

EXPOSE 7860
CMD ["python", "app.py"]