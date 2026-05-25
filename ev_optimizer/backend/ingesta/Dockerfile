# ingesta/Dockerfile
FROM python:3.11-slim

RUN apt-get update && apt-get install -y cron && rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN pip install --no-cache-dir requests pymongo python-dotenv

COPY pvpc_ingesta.py .

RUN mkdir -p /data

# Cron: ejecuta la ingesta cada día a las 00:15
RUN echo "45 20 * * * cd /app && /usr/local/bin/python3 pvpc_ingesta.py >> /var/log/ingesta.log 2>&1" > /etc/cron.d/ingesta \
    && chmod 0644 /etc/cron.d/ingesta \
    && crontab /etc/cron.d/ingesta

CMD ["cron", "-f"]
