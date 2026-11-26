FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
# Phase 3.5.2 / F2: presny commit, ze ktereho image vznikl (predava scripts/build_release.py). Bez nej build selze –
# verze se za behu z Gitu neodhaduje (v image .git neni).
ARG TIPSBOT_CODE_REV
RUN case "$TIPSBOT_CODE_REV" in \
      [0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]*) ;; \
      *) echo "TIPSBOT_CODE_REV chybi nebo neni commit (build spoustej pres scripts/build_release.py)" >&2; exit 1 ;; \
    esac
ENV TIPSBOT_CODE_REV=$TIPSBOT_CODE_REV
LABEL org.opencontainers.image.revision=$TIPSBOT_CODE_REV
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install ".[second]"
# data (databaze, klice, stazena data) se pripojuji zvenku, do obrazu nepatri
CMD ["tipsbot", "web", "--host", "0.0.0.0", "--port", "8000"]
