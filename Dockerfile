FROM python:3.12-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends cron tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY . /app

RUN mkdir -p /app/logs /app/data \
    && touch /app/logs/cron.log \
    && sed -i 's/\r$//' /app/cron/scheduler.cron \
    && crontab /app/cron/scheduler.cron \
    && chmod +x /app/entrypoint.sh \
    && true

ENV TZ=Europe/Athens

ENTRYPOINT ["/app/entrypoint.sh"]
