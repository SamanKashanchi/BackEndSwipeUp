# DoomSwipe Backend
Supabase Postgres + pgvector backend.

## Setup

1. `pip install -r requirements.txt`
2. Create `.env` with `DATABASE_URL=postgresql://...` (Supabase Session pooler connection string)
3. In the Supabase dashboard, enable the `vector` extension (Database -> Extensions)
4. `python test_connection.py`
