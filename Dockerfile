FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

RUN useradd --create-home --uid 1000 bot
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot ./bot
COPY config/config.example.yaml ./config/config.example.yaml
RUN mkdir -p /app/data && chown -R bot:bot /app
USER bot

ENTRYPOINT ["python", "-m", "bot"]
CMD ["run"]
