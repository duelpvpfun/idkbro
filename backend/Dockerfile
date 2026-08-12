FROM python:3.12-slim

WORKDIR /app

# Install deps first for better layer caching.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# The SQLite DB + logs live here; mount a volume so they survive container restarts.
VOLUME ["/app/data"]

EXPOSE 8000

# Run the agent (serves the dashboard + API on :8000).
CMD ["python", "-m", "app.main"]
