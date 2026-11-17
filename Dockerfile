FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install ".[second]"
# data (databaze, klice, stazena data) se pripojuji zvenku, do obrazu nepatri
CMD ["tipsbot", "web", "--host", "0.0.0.0", "--port", "8000"]
