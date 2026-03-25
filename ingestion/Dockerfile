FROM python:3.11-slim

WORKDIR /app

# Install build dependencies for psycopg2
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy ingestion source
COPY . .

# Copy shared migrations directory (mounted at runtime via volume in docker-compose)
# but ensure alembic.ini is present for migration runs
COPY alembic.ini /app/alembic.ini

CMD ["python", "main.py"]
