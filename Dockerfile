ARG TARGETARCH
ARG CHATGPT2API_CLEANUP_ATTESTATION_TRUST_ANCHOR_SHA256=""

FROM node:22-alpine AS web-build

WORKDIR /app/web-vue

COPY web-vue/package.json web-vue/package-lock.json ./
RUN npm ci

COPY VERSION /app/VERSION
COPY CHANGELOG.md /app/CHANGELOG.md
COPY web-vue ./
RUN npm run build


FROM python:3.13-slim AS app

ARG TARGETARCH
ARG CHATGPT2API_CLEANUP_ATTESTATION_TRUST_ANCHOR_SHA256=""

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    TZ=Asia/Shanghai \
    CHATGPT2API_THREAD_TOKENS=80 \
    GENBOX_CLEANUP_PROTECTED_STAGING_ROOT=/app/.genbox-cleanup-staging

WORKDIR /app

# Cleanup staging is deliberately owned by a different uid and is not
# writable through the image volume. POSIX source deletion remains fail-closed
# when this boundary is missing or misconfigured.
RUN install -d -o nobody -g nogroup -m 700 /app/.genbox-cleanup-staging

# 安装系统依赖
# - git: Git 存储后端需要
# - libpq-dev: PostgreSQL 客户端库
# - gcc: 编译 psycopg2-binary 需要
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    libpq-dev \
    gcc \
    openssl \
    tzdata \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY main.py ./
COPY config.example.yaml ./
COPY VERSION ./
COPY api ./api
COPY services ./services
# The public-key fingerprint becomes part of the immutable application image.
# The private signing key is never copied into this image.
RUN python -c "import re, sys; from pathlib import Path; value = sys.argv[1].strip().lower(); assert not value or re.fullmatch(r'[0-9a-f]{64}', value), 'trust anchor must be an empty value or SHA-256'; Path('services/cleanup_attestation_anchor.py').write_text(f'CLEANUP_ATTESTATION_PUBLIC_KEY_SHA256 = {value!r}\\n', encoding='ascii')" "$CHATGPT2API_CLEANUP_ATTESTATION_TRUST_ANCHOR_SHA256"
COPY utils ./utils
COPY scripts ./scripts
COPY --from=web-build /app/web-vue/dist ./web_dist

EXPOSE 80

CMD ["uv", "run", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "80", "--access-log"]
