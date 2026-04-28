# SwipeUp API (consumer-facing)

FastAPI service backing the consumer webapp. Lives at `Backend/api/`,
deploys to Cloud Run as `swipeup-api`.

## Endpoints

- `GET  /health` — readiness probe (also checks Postgres connectivity)
- `DELETE /account/{account_id}` — wipe an account's Postgres footprint
  (cascades to summary + frame embeddings + interactions). Requires
  `Authorization: Bearer <firebase-id-token>` from the account owner.

Future (per `Backend/CONTEXT.md` §5.1):
- `GET /feed?account_id=X&limit=50&cursor=...`
- `POST /swipe`
- `POST /view`

## Local dev

```bash
cd Backend/api
pip install -r requirements.txt
# Backend/.env must contain DATABASE_URL
uvicorn main:app --reload --port 8000
curl http://localhost:8000/health
```

For the auth-protected endpoints to work locally you need ADC:

```bash
gcloud auth application-default login
```

## Deploy to Cloud Run

```bash
cd Backend/api
gcloud run deploy swipeup-api \
  --source . \
  --region us-central1 \
  --project reel-swipe-app \
  --allow-unauthenticated \
  --set-env-vars "DATABASE_URL=<paste from Backend/.env>"
```

`--allow-unauthenticated` makes the service publicly reachable (Cloud Run
auth disabled); the actual auth happens at the application layer via the
Firebase ID token check.
