FROM python:3.10-slim

WORKDIR /app

# System deps for opencv + video decode. psycopg[binary] ships libpq in the
# wheel so no libpq-dev/gcc needed.
RUN apt-get update && apt-get install -y \
    ffmpeg \
    libgl1 \
    && rm -rf /var/lib/apt/lists/*

# Copy files
COPY . .

# Install Python deps
RUN pip install --no-cache-dir --upgrade pip
RUN pip install --no-cache-dir -r requirements.txt

# Pre-download SigLIP + DINOv2 models during build so Cloud Run cold starts
# don't pay for HuggingFace downloads.
RUN python -c "from transformers import AutoTokenizer, AutoModel, AutoProcessor, AutoImageProcessor; \
    s='google/siglip-so400m-patch14-384'; d='facebook/dinov2-base'; \
    AutoTokenizer.from_pretrained(s); \
    AutoProcessor.from_pretrained(s); \
    AutoModel.from_pretrained(s); \
    AutoImageProcessor.from_pretrained(d); \
    AutoModel.from_pretrained(d); \
    print('Models downloaded successfully')"

# Cloud Run expects port 8080.
ENV PORT=8080

# REQUIRED runtime env vars (set in Cloud Run, NOT baked into the image):
#   DATABASE_URL   — Supabase Postgres Session pooler URL
#                    e.g. postgresql://postgres.<project>:<pw>@aws-1-us-east-1.pooler.supabase.com:5432/postgres
#   HF_TOKEN       — Hugging Face token for SigLIP model download (optional but recommended)
#   APIFY_TOKEN    — Apify API token for Instagram scraping (falls back to hardcoded default if unset)

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8080"]
