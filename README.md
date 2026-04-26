# Landing Page — Phone Cases (Mexico)

Standalone FastAPI landing page for the Terex phone case business. Reads the same Supabase project as the inventory app (`cxmex/inventoriorapido1`), so stock and sales data stay in sync without duplication.

## Run locally

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then fill in real values
uvicorn app:app --reload --port 8000
```

Open http://localhost:8000

## Deploy (Railway)

`Procfile` is configured. Set the same env vars from `.env.example` in the Railway project.

## A/B testing

The root route `/` randomly assigns visitors to variants A, B, or C and 302-redirects to `/v/<variant>`. Variant assignment + every page event is logged to `landing_events`.

URL params honored everywhere:
- `?wa=<phone>` — WhatsApp number of the visitor (passed in by ads / WhatsApp links so we can correlate sessions)
- `?v=A|B|C` — force a specific variant (overrides random assignment, useful for ad campaigns)
- `?utm_source`, `?utm_campaign` — standard, stored on every event

## Database

Run `migrations.sql` in the Supabase SQL editor once. It creates:
- `landing_events` — page views, variant assignments, clicks, cart adds
- `landing_leads` — captured emails / phone numbers
- `landing_carts` — server-side cart state (keyed by session)

The catalog tables (`inventario`, `inventario_modelos`, `inventario_estilos`) are owned by the inventory app — this app reads them but does not write.
