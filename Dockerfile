FROM python:3.12-slim

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# SQLite file persists here; mount a volume to this path in production
# so data survives container restarts/redeploys.
VOLUME ["/srv/data"]
ENV APEX_DB_PATH=/srv/data/apex_pilot.db

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
