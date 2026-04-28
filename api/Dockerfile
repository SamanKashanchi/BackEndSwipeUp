FROM python:3.11-slim

WORKDIR /app

# psycopg[binary] ships libpq in the wheel — no apt deps needed.

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir -r requirements.txt

COPY . .

# Cloud Run injects PORT; default to 8080 for local docker run.
ENV PORT=8080

# REQUIRED env vars at runtime (set on Cloud Run, NOT baked into the image):
#   DATABASE_URL          Supabase Postgres session-pooler URL
# OPTIONAL:
#   FIREBASE_PROJECT_ID   defaults to reel-swipe-app

CMD exec uvicorn main:app --host 0.0.0.0 --port ${PORT}
