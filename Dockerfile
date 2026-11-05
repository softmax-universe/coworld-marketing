FROM docker.io/library/python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY marketing ./marketing
COPY docs/POLICY_GUIDE.md ./docs/POLICY_GUIDE.md
RUN pip install --no-cache-dir .

CMD ["python", "-m", "marketing.server"]
