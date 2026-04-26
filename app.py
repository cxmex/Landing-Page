"""
Landing Page — Phone Cases (Mexico).

Standalone FastAPI app that reads the same Supabase project as the inventory app
(`cxmex/inventoriorapido1`). Owns its own A/B testing, event logging, and cart tables.
"""

import os
import re
import time
import secrets
import asyncio
from typing import Optional

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Request, HTTPException, Form, Depends
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.security import HTTPBasic, HTTPBasicCredentials
import random

load_dotenv()

SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://gbkhkbfbarsnpbdkxzii.supabase.co")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
WHATSAPP_NUMBER = os.environ.get("WHATSAPP_NUMBER", "525545174085")
WHATSAPP_DEFAULT_MESSAGE = os.environ.get(
    "WHATSAPP_DEFAULT_MESSAGE",
    "Hola, vi su catalogo de fundas y me interesa",
)
BUSINESS_PHONE = os.environ.get("BUSINESS_PHONE", "525545174085")
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
PUBLIC_URL = os.environ.get("PUBLIC_URL", "")  # for OG tags; e.g. https://landing.terex.mx

if not SUPABASE_KEY:
    raise RuntimeError(
        "SUPABASE_KEY not set. Copy .env.example to .env and fill in the anon key."
    )

HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "return=representation",
}

VARIANTS = ("A", "B", "C")
SESSION_COOKIE = "lp_sid"
VARIANT_COOKIE = "lp_variant"
MODELOS_CACHE_TTL = 300  # 5 minutes

app = FastAPI(title="Phone Cases — Landing Page")
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")
basic_auth = HTTPBasic()


# ──────────────────────────────────────────────────────────────────────────────
# Supabase helpers
# ──────────────────────────────────────────────────────────────────────────────

async def supabase_get(path: str, params: Optional[dict] = None, range_header: str = "0-9999"):
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{SUPABASE_URL}/rest/v1/{path}",
            headers={**HEADERS, "Range": range_header},
            params=params or {},
        )
    if resp.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"Supabase GET {path}: {resp.text}")
    return resp.json()


async def supabase_post(path: str, json_data, params: Optional[dict] = None):
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"{SUPABASE_URL}/rest/v1/{path}",
            headers=HEADERS,
            params=params or {},
            json=json_data,
        )
    if resp.status_code >= 400:
        # Don't crash the page on logging errors — log and continue.
        print(f"[supabase_post {path}] {resp.status_code} {resp.text}")
        return None
    return resp.json()


async def supabase_rpc(name: str, params: Optional[dict] = None):
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{SUPABASE_URL}/rest/v1/rpc/{name}",
            headers=HEADERS,
            json=params or {},
        )
    if resp.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"RPC {name}: {resp.text}")
    return resp.json()


# ──────────────────────────────────────────────────────────────────────────────
# Session + variant assignment
# ──────────────────────────────────────────────────────────────────────────────

def get_or_create_session_id(request: Request) -> str:
    sid = request.cookies.get(SESSION_COOKIE)
    if not sid:
        sid = secrets.token_urlsafe(16)
    return sid


def assign_variant(request: Request, override: Optional[str]) -> str:
    if override and override.upper() in VARIANTS:
        return override.upper()
    cookie_v = request.cookies.get(VARIANT_COOKIE)
    if cookie_v in VARIANTS:
        return cookie_v
    return random.choice(VARIANTS)


async def log_event(
    request: Request,
    session_id: str,
    variant: Optional[str],
    event_type: str,
    **fields,
):
    payload = {
        "session_id": session_id,
        "variant": variant,
        "event_type": event_type,
        "user_agent": request.headers.get("user-agent", "")[:500],
        "referrer": request.headers.get("referer", "")[:500],
        "utm_source": request.query_params.get("utm_source"),
        "utm_campaign": request.query_params.get("utm_campaign"),
        "utm_medium": request.query_params.get("utm_medium"),
        "wa_phone": request.query_params.get("wa"),
    }
    payload.update({k: v for k, v in fields.items() if v is not None})
    await supabase_post("landing_events", [payload])


def set_session_cookies(response, session_id: str, variant: Optional[str] = None):
    response.set_cookie(SESSION_COOKIE, session_id, max_age=60 * 60 * 24 * 90, httponly=True, samesite="lax")
    if variant:
        response.set_cookie(VARIANT_COOKIE, variant, max_age=60 * 60 * 24 * 30, samesite="lax")


# ──────────────────────────────────────────────────────────────────────────────
# Catalog data (ported from inventory app's /api/browse-modelo)
# ──────────────────────────────────────────────────────────────────────────────

# In-memory cache for modelos list — fetched on every page render otherwise.
# Acceptable since the data is small (<5 KB), changes infrequently, and a 5-min
# stale window is fine for a public landing page.
_modelos_cache: dict = {"data": None, "ts": 0}
_modelos_lock = asyncio.Lock()


async def fetch_modelos():
    now = time.time()
    if _modelos_cache["data"] is not None and (now - _modelos_cache["ts"]) < MODELOS_CACHE_TTL:
        return _modelos_cache["data"]

    async with _modelos_lock:
        # double-check after acquiring lock
        if _modelos_cache["data"] is not None and (time.time() - _modelos_cache["ts"]) < MODELOS_CACHE_TTL:
            return _modelos_cache["data"]
        data = await supabase_get(
            "inventario_modelos",
            params={"select": "id,modelo,marca", "order": "modelo.asc"},
        )
        _modelos_cache["data"] = data
        _modelos_cache["ts"] = time.time()
        return data


def slugify(text: str) -> str:
    """URL-safe slug: lowercase, hyphens, alphanumeric only."""
    s = text.strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")


async def fetch_products_for_modelo(modelo: str, days: int = 30):
    """Return estilo+color products for a modelo with images and metrics."""
    async with httpx.AsyncClient(timeout=30) as client:
        resp_analysis, resp_images, resp_estilos, resp_colors = await asyncio.gather(
            client.get(
                f"{SUPABASE_URL}/rest/v1/rpc/get_order_analysis",
                headers={**HEADERS, "Range": "0-9999"},
                params={"days_back": days},
            ),
            client.get(
                f"{SUPABASE_URL}/rest/v1/image_uploads",
                headers={**HEADERS, "Range": "0-9999"},
                params={"select": "estilo_id,color_id,public_url"},
            ),
            client.get(
                f"{SUPABASE_URL}/rest/v1/inventario_estilos",
                headers={**HEADERS, "Range": "0-9999"},
                params={"select": "id,nombre"},
            ),
            client.get(
                f"{SUPABASE_URL}/rest/v1/inventario_colores",
                headers={**HEADERS, "Range": "0-9999"},
                params={"select": "id,color", "order": "color.asc"},
            ),
        )

    if resp_analysis.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"RPC error: {resp_analysis.text}")

    all_rows = resp_analysis.json()

    estilo_id_map = {
        e["nombre"]: e["id"]
        for e in (resp_estilos.json() if resp_estilos.status_code < 400 else [])
    }

    images_by_estilo: dict[int, dict[int, str]] = {}
    if resp_images.status_code < 400:
        for img in resp_images.json():
            eid, cid, url = img.get("estilo_id"), img.get("color_id"), img.get("public_url", "")
            if eid and url:
                images_by_estilo.setdefault(eid, {}).setdefault(cid, url)

    color_name_to_id = {
        c["color"].strip().upper(): c["id"]
        for c in (resp_colors.json() if resp_colors.status_code < 400 else [])
    }

    target = modelo.strip().upper()
    filtered = [
        r for r in all_rows
        if (r.get("modelo") or "").strip().upper() == target
        and (float(r.get("stock_total", 0) or 0) > 0
             or float(r.get("sold_total", 0) or 0) > 0)
    ]

    products = []
    for r in filtered:
        est = r.get("estilo", "") or "Sin estilo"
        color = r.get("color", "") or "Sin color"
        stock = int(float(r.get("stock_total", 0) or 0))
        sold = int(float(r.get("sold_total", 0) or 0))
        rev = float(r.get("revenue_total", 0) or 0)
        avg_daily = float(r.get("avg_daily_sales", 0) or 0)
        doi_raw = r.get("days_of_inventory")
        doi = round(float(doi_raw), 1) if doi_raw is not None else None

        eid = estilo_id_map.get(est)
        cid = color_name_to_id.get(color.strip().upper())
        image_url = ""
        if eid and cid and eid in images_by_estilo:
            image_url = images_by_estilo[eid].get(cid, "")
        if not image_url and eid and eid in images_by_estilo:
            imgs = images_by_estilo[eid]
            if imgs:
                image_url = next(iter(imgs.values()), "")

        products.append({
            "estilo": est,
            "color": color,
            "stock": stock,
            "sold": sold,
            "revenue": round(rev),
            "avg_daily": round(avg_daily, 1),
            "doi": doi,
            "image_url": image_url,
            "has_image": bool(image_url),
        })

    products.sort(key=lambda p: (-int(p["has_image"]), -p["sold"]))
    return products


def shared_template_ctx(request: Request, **extra) -> dict:
    return {
        "wa_number": WHATSAPP_NUMBER,
        "wa_default_msg": WHATSAPP_DEFAULT_MESSAGE,
        "business_phone": BUSINESS_PHONE,
        "public_url": PUBLIC_URL or str(request.base_url).rstrip("/"),
        **extra,
    }


# ──────────────────────────────────────────────────────────────────────────────
# Routes — A/B router
# ──────────────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def root(request: Request, v: Optional[str] = None):
    """Assigns the visitor to a variant and redirects, preserving query params."""
    session_id = get_or_create_session_id(request)
    variant = assign_variant(request, v)

    qs = dict(request.query_params)
    qs.pop("v", None)
    qs_str = ("?" + "&".join(f"{k}={val}" for k, val in qs.items())) if qs else ""

    response = RedirectResponse(url=f"/v/{variant}{qs_str}", status_code=302)
    set_session_cookies(response, session_id, variant)
    return response


@app.get("/v/{variant}", response_class=HTMLResponse)
async def variant_page(request: Request, variant: str):
    variant = variant.upper()
    if variant not in VARIANTS:
        raise HTTPException(status_code=404, detail="Unknown variant")

    session_id = get_or_create_session_id(request)
    modelos = await fetch_modelos()

    await log_event(request, session_id, variant, "pageview")

    response = templates.TemplateResponse(
        request=request,
        name=f"variant_{variant.lower()}.html",
        context=shared_template_ctx(
            request,
            modelos=modelos,
            variant=variant,
            session_id=session_id,
        ),
    )
    set_session_cookies(response, session_id, variant)
    return response


@app.get("/p/{slug}", response_class=HTMLResponse)
async def phone_model_deep_link(request: Request, slug: str):
    """SEO-friendly + ad-friendly deep link, e.g. /p/iphone-15.

    Resolves slug to a modelo, redirects to the assigned variant with `?modelo=` set.
    """
    modelos = await fetch_modelos()
    target_slug = slugify(slug)
    matched = next((m for m in modelos if slugify(m["modelo"]) == target_slug), None)
    if not matched:
        # try partial match
        matched = next(
            (m for m in modelos if target_slug in slugify(m["modelo"])),
            None,
        )

    session_id = get_or_create_session_id(request)
    variant = assign_variant(request, request.query_params.get("v"))

    qs = dict(request.query_params)
    qs.pop("v", None)
    if matched:
        qs["modelo"] = matched["modelo"]
    qs_str = ("?" + "&".join(f"{k}={val}" for k, val in qs.items())) if qs else ""

    response = RedirectResponse(url=f"/v/{variant}{qs_str}", status_code=302)
    set_session_cookies(response, session_id, variant)
    return response


@app.get("/mayoreo", response_class=HTMLResponse)
async def mayoreo_page(request: Request):
    """Dedicated wholesale lead-capture page. Variant-independent — useful for
    targeted ads aimed at resellers / business buyers."""
    session_id = get_or_create_session_id(request)
    await log_event(request, session_id, None, "pageview", metadata={"page": "mayoreo"})
    response = templates.TemplateResponse(
        request=request,
        name="mayoreo.html",
        context=shared_template_ctx(request, session_id=session_id),
    )
    set_session_cookies(response, session_id)
    return response


# ──────────────────────────────────────────────────────────────────────────────
# Routes — JSON APIs
# ──────────────────────────────────────────────────────────────────────────────

@app.get("/api/products")
async def api_products(request: Request, modelo: str, days: int = 30):
    products = await fetch_products_for_modelo(modelo, days)
    session_id = get_or_create_session_id(request)
    variant = request.cookies.get(VARIANT_COOKIE)
    await log_event(request, session_id, variant, "search", modelo=modelo, metadata={"result_count": len(products)})
    return JSONResponse({"products": products, "modelo": modelo, "count": len(products)})


@app.post("/api/track")
async def api_track(request: Request):
    """Generic event ingestion from the client (clicks, cart adds, WhatsApp clicks)."""
    body = await request.json()
    session_id = get_or_create_session_id(request)
    variant = request.cookies.get(VARIANT_COOKIE)
    event_type = body.get("event_type") or "click"
    allowed = {
        "click", "add_to_cart", "remove_from_cart", "whatsapp_click",
        "call_click", "lead_submit", "bounce", "search",
    }
    if event_type not in allowed:
        raise HTTPException(status_code=400, detail=f"Unknown event_type: {event_type}")

    await log_event(
        request, session_id, variant, event_type,
        modelo=body.get("modelo"),
        estilo=body.get("estilo"),
        color=body.get("color"),
        barcode=body.get("barcode"),
        quantity=body.get("quantity"),
        metadata=body.get("metadata"),
    )
    return {"ok": True}


@app.post("/api/lead")
async def api_lead(
    request: Request,
    email: Optional[str] = Form(None),
    phone: Optional[str] = Form(None),
    name: Optional[str] = Form(None),
    customer_type: Optional[str] = Form(None),
    source: Optional[str] = Form("inline_form"),
    notes: Optional[str] = Form(None),
):
    if not email and not phone:
        raise HTTPException(status_code=400, detail="email or phone required")
    session_id = get_or_create_session_id(request)
    variant = request.cookies.get(VARIANT_COOKIE)
    payload = {
        "session_id": session_id,
        "variant": variant,
        "email": email,
        "phone": phone,
        "name": name,
        "customer_type": customer_type or "unknown",
        "source": source,
        "utm_source": request.query_params.get("utm_source"),
        "utm_campaign": request.query_params.get("utm_campaign"),
        "metadata": {"notes": notes} if notes else None,
    }
    await supabase_post("landing_leads", [payload])
    await log_event(request, session_id, variant, "lead_submit", metadata={"source": source})
    return {"ok": True}


# ──────────────────────────────────────────────────────────────────────────────
# Admin
# ──────────────────────────────────────────────────────────────────────────────

def require_admin(credentials: HTTPBasicCredentials = Depends(basic_auth)):
    if not ADMIN_PASSWORD:
        raise HTTPException(
            status_code=503,
            detail="ADMIN_PASSWORD not set in env. Admin pages disabled.",
        )
    user_ok = secrets.compare_digest(credentials.username.encode(), ADMIN_USERNAME.encode())
    pass_ok = secrets.compare_digest(credentials.password.encode(), ADMIN_PASSWORD.encode())
    if not (user_ok and pass_ok):
        raise HTTPException(
            status_code=401,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


@app.get("/admin/funnel", response_class=HTMLResponse)
async def admin_funnel(request: Request, days: int = 14, _user: str = Depends(require_admin)):
    """A/B test funnel readout — variant comparison from landing_funnel_summary RPC."""
    try:
        summary = await supabase_rpc("landing_funnel_summary", {"p_days": days})
    except HTTPException:
        # RPC may not exist yet — show empty state with hint to run migrations
        summary = []

    total_leads = await supabase_get(
        "landing_leads",
        params={"select": "id", "order": "created_at.desc"},
        range_header="0-9999",
    )
    recent_events = await supabase_get(
        "landing_events",
        params={"select": "created_at,variant,event_type,modelo,wa_phone,utm_source,utm_campaign",
                "order": "created_at.desc"},
        range_header="0-99",
    )

    return templates.TemplateResponse(
        request=request,
        name="admin_funnel.html",
        context={
            "summary": summary,
            "days": days,
            "total_leads": len(total_leads),
            "recent_events": recent_events,
        },
    )


# ──────────────────────────────────────────────────────────────────────────────
# Misc / SEO
# ──────────────────────────────────────────────────────────────────────────────

@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.get("/robots.txt", response_class=PlainTextResponse)
async def robots():
    return "User-agent: *\nAllow: /\nDisallow: /admin/\nDisallow: /api/\n"


@app.exception_handler(404)
async def not_found_handler(request: Request, exc: HTTPException):
    # Only render the HTML 404 for browser navigations, JSON for API paths.
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": "Not Found"}, status_code=404)
    return templates.TemplateResponse(
        request=request,
        name="404.html",
        context=shared_template_ctx(request, session_id=get_or_create_session_id(request)),
        status_code=404,
    )
