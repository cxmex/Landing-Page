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
from urllib.parse import quote_plus
from dotenv import load_dotenv
from fastapi import FastAPI, Request, HTTPException, Form, Depends, BackgroundTasks
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.security import HTTPBasic, HTTPBasicCredentials
import random

load_dotenv()

SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://gbkhkbfbarsnpbdkxzii.supabase.co")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
# service_role key bypasses RLS — used only for admin dashboard reads. If unset,
# falls back to SUPABASE_KEY (which works only if RLS is off or grants anon SELECT).
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "") or SUPABASE_KEY
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
    # return=minimal: PostgREST skips the post-insert SELECT. Required because
    # landing_events/landing_leads have no SELECT policy for anon (by design —
    # protects lead data), so return=representation triggers an RLS rollback
    # and silently drops every write.
    "Prefer": "return=minimal",
}

ADMIN_HEADERS = {
    "apikey": SUPABASE_SERVICE_KEY,
    "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "return=representation",
}

VARIANTS = ("A", "B", "C")
SESSION_COOKIE = "lp_sid"
VARIANT_COOKIE = "lp_variant"
MODELOS_CACHE_TTL = 300  # 5 minutes
BESTSELLERS_CACHE_TTL = 300  # 5 minutes

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
    # 201 with return=minimal returns no body; callers don't use the response.
    return None


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


# Public image proxy: resizes to display dimensions and re-encodes to WebP.
# Source images on Supabase Storage are full-resolution JPEGs with cache-control:
# no-cache (every request goes back to origin). Proxying through wsrv.nl gives us
# WebP at the requested width and a 1-year edge cache header. Free, no signup.
# Set IMAGE_PROXY_BASE='' in env to disable (templates fall back to image_url).
IMAGE_PROXY_BASE = os.environ.get(
    "IMAGE_PROXY_BASE",
    "https://wsrv.nl/?url={url}&w={w}&output=webp&q=75&we",
)


def image_proxy_url(original: str, width: int = 400) -> str:
    """Wrap a public image URL in the configured proxy. Empty in → empty out."""
    if not original or not IMAGE_PROXY_BASE:
        return original or ""
    return IMAGE_PROXY_BASE.format(url=quote_plus(original), w=width)


_bestsellers_cache: dict = {"data": None, "ts": 0, "key": None}
_bestsellers_lock = asyncio.Lock()


async def fetch_bestsellers(limit: int = 12, days: int = 30):
    """Top-selling estilo+color products across all modelos, with images.

    Reads the `landing_bestsellers` materialized view (see
    migrations_bestsellers_mv.sql), which is refreshed by pg_cron every 30 min.
    Single indexed SELECT — typically <50ms. Falls back to [] on error so a
    Supabase outage never breaks the landing page.

    `days` is ignored at runtime (the MV is built with a fixed window); it stays
    in the signature so callers don't break. To change the window, re-deploy
    the MV with the new constant.
    """
    cache_key = (limit,)
    now = time.time()
    if (_bestsellers_cache["data"] is not None
            and _bestsellers_cache["key"] == cache_key
            and (now - _bestsellers_cache["ts"]) < BESTSELLERS_CACHE_TTL):
        return _bestsellers_cache["data"]

    async with _bestsellers_lock:
        if (_bestsellers_cache["data"] is not None
                and _bestsellers_cache["key"] == cache_key
                and (time.time() - _bestsellers_cache["ts"]) < BESTSELLERS_CACHE_TTL):
            return _bestsellers_cache["data"]

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    f"{SUPABASE_URL}/rest/v1/landing_bestsellers",
                    headers=HEADERS,
                    params={
                        "select": "estilo,color,modelo,stock,sold,image_url",
                        "order": "sold.desc",
                        "limit": str(limit),
                    },
                )
            if resp.status_code >= 400:
                print(f"[fetch_bestsellers] {resp.status_code} {resp.text[:200]}")
                return []
            data = resp.json() or []
        except Exception as e:
            print(f"[fetch_bestsellers] {type(e).__name__}: {e}")
            return []

        for r in data:
            r["image_url_thumb"] = image_proxy_url(r.get("image_url", ""), width=400)

        _bestsellers_cache["data"] = data
        _bestsellers_cache["ts"] = time.time()
        _bestsellers_cache["key"] = cache_key
        return data


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
            "image_url_thumb": image_proxy_url(image_url, width=400),
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


async def fetch_session_identity(session_id: str) -> dict:
    """Look up what we already know about this session from landing_carts.

    Used so the bounce modal can pre-fill known email/phone and skip showing
    itself when we already have both. Returns {} on any error or empty result —
    this is decoration, never block the page render on it.
    """
    if not session_id:
        return {}
    try:
        async with httpx.AsyncClient(timeout=4) as client:
            resp = await client.get(
                f"{SUPABASE_URL}/rest/v1/landing_carts",
                headers=HEADERS,
                params={
                    "select": "email,wa_phone,customer_type,items",
                    "session_id": f"eq.{session_id}",
                    "limit": "1",
                },
            )
        if resp.status_code >= 400 or not resp.json():
            return {}
        row = resp.json()[0]
        items = row.get("items") or []
        return {
            "email": row.get("email") or "",
            "phone": row.get("wa_phone") or "",
            "customer_type": row.get("customer_type") or "",
            "has_cart_items": bool(items),
        }
    except Exception as e:
        print(f"[fetch_session_identity] {type(e).__name__}: {e}")
        return {}


async def upsert_cart_identity(
    session_id: str,
    email: Optional[str] = None,
    phone: Optional[str] = None,
    customer_type: Optional[str] = None,
    variant: Optional[str] = None,
) -> None:
    """Persist captured identity onto landing_carts so the cart row carries it
    across visits. Only sends columns that have values, so existing items[] /
    status are not blanked out by a lead-side write."""
    payload: dict = {"session_id": session_id}
    if email:         payload["email"] = email
    if phone:         payload["wa_phone"] = phone
    if customer_type: payload["customer_type"] = customer_type
    if variant:       payload["variant"] = variant
    if len(payload) == 1:
        return  # nothing to write besides the conflict key
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{SUPABASE_URL}/rest/v1/landing_carts",
                headers={**HEADERS, "Prefer": "resolution=merge-duplicates,return=minimal"},
                params={"on_conflict": "session_id"},
                json=[payload],
            )
        if resp.status_code >= 400:
            print(f"[upsert_cart_identity] {resp.status_code} {resp.text[:200]}")
    except Exception as e:
        print(f"[upsert_cart_identity] {type(e).__name__}: {e}")


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
async def variant_page(request: Request, variant: str, background_tasks: BackgroundTasks):
    variant = variant.upper()
    if variant not in VARIANTS:
        raise HTTPException(status_code=404, detail="Unknown variant")

    session_id = get_or_create_session_id(request)
    # Run modelos + identity lookup concurrently — both are independent reads.
    modelos, known_identity = await asyncio.gather(
        fetch_modelos(),
        fetch_session_identity(session_id),
    )

    background_tasks.add_task(log_event, request, session_id, variant, "pageview")

    response = templates.TemplateResponse(
        request=request,
        name=f"variant_{variant.lower()}.html",
        context=shared_template_ctx(
            request,
            modelos=modelos,
            variant=variant,
            session_id=session_id,
            known_identity=known_identity,
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
async def mayoreo_page(request: Request, background_tasks: BackgroundTasks):
    """Dedicated wholesale lead-capture page. Variant-independent — useful for
    targeted ads aimed at resellers / business buyers."""
    session_id = get_or_create_session_id(request)
    known_identity = await fetch_session_identity(session_id)
    background_tasks.add_task(log_event, request, session_id, None, "pageview", metadata={"page": "mayoreo"})
    response = templates.TemplateResponse(
        request=request,
        name="mayoreo.html",
        context=shared_template_ctx(request, session_id=session_id, known_identity=known_identity),
    )
    set_session_cookies(response, session_id)
    return response


# ──────────────────────────────────────────────────────────────────────────────
# Routes — JSON APIs
# ──────────────────────────────────────────────────────────────────────────────

@app.get("/api/products")
async def api_products(request: Request, background_tasks: BackgroundTasks, modelo: str, days: int = 30):
    products = await fetch_products_for_modelo(modelo, days)
    session_id = get_or_create_session_id(request)
    variant = request.cookies.get(VARIANT_COOKIE)
    background_tasks.add_task(log_event, request, session_id, variant, "search", modelo=modelo, metadata={"result_count": len(products)})
    return JSONResponse({"products": products, "modelo": modelo, "count": len(products)})


@app.get("/api/bestsellers")
async def api_bestsellers(limit: int = 12, days: int = 30):
    products = await fetch_bestsellers(limit=min(limit, 24), days=days)
    return JSONResponse({"products": products, "count": len(products)})


@app.post("/api/track")
async def api_track(request: Request, background_tasks: BackgroundTasks):
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

    background_tasks.add_task(
        log_event,
        request, session_id, variant, event_type,
        modelo=body.get("modelo"),
        estilo=body.get("estilo"),
        color=body.get("color"),
        barcode=body.get("barcode"),
        quantity=body.get("quantity"),
        metadata=body.get("metadata"),
    )
    return {"ok": True}


@app.post("/api/cart")
async def api_cart_save(request: Request):
    """Persist cart contents to landing_carts (upsert by session_id).
    Called on every add/remove from the client so we can recover
    abandoned carts and surface 'active baskets' in the admin dashboard."""
    body = await request.json()
    session_id = get_or_create_session_id(request)
    variant = request.cookies.get(VARIANT_COOKIE)
    items = body.get("items") or []
    if not isinstance(items, list):
        raise HTTPException(400, "items must be a list")
    customer_type = (body.get("customer_type") or "unknown")[:32]
    from datetime import datetime, timezone
    payload = {
        "session_id": session_id,
        "variant": variant,
        "items": items,
        "customer_type": customer_type,
        "status": body.get("status") or "open",
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    # Upsert via Prefer: resolution=merge-duplicates on session_id (unique)
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"{SUPABASE_URL}/rest/v1/landing_carts",
            headers={**HEADERS, "Prefer": "resolution=merge-duplicates,return=minimal"},
            params={"on_conflict": "session_id"},
            json=[payload],
        )
    if resp.status_code >= 400:
        print(f"[api_cart] {resp.status_code} {resp.text}")
    return {"ok": True, "session_id": session_id}


@app.post("/api/lead")
async def api_lead(
    request: Request,
    background_tasks: BackgroundTasks,
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
    # Persist identity on the cart row so this session is recognized on return
    # visits (cart row holds the identity across pageviews; lead row is the log).
    background_tasks.add_task(
        upsert_cart_identity,
        session_id, email, phone, customer_type or "unknown", variant,
    )
    background_tasks.add_task(log_event, request, session_id, variant, "lead_submit", metadata={"source": source})
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


async def _admin_supabase_get(path: str, params: dict, range_header: str = "0-9999"):
    """Admin reads use service_role key (bypasses RLS)."""
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{SUPABASE_URL}/rest/v1/{path}",
            headers={**ADMIN_HEADERS, "Range": range_header},
            params=params,
        )
    if resp.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"Admin GET {path}: {resp.text}")
    return resp.json()


async def _admin_supabase_rpc(name: str, params: dict):
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{SUPABASE_URL}/rest/v1/rpc/{name}",
            headers=ADMIN_HEADERS,
            json=params,
        )
    if resp.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"Admin RPC {name}: {resp.text}")
    return resp.json()


def _aggregate_funnel(events: list, leads: list) -> dict:
    """Compute funnel breakdowns from raw event/lead lists.
    Cheap because volume is small — at hundreds of thousands we'd push this
    into Postgres views, but for an A/B test landing page it's fine."""
    from collections import Counter, defaultdict

    # Per-variant step counts (sessions, pageviews, search, add_to_cart, wa_click, lead)
    sessions_by_variant = defaultdict(set)
    step_counts = defaultdict(lambda: Counter())  # step_counts[variant][event_type] = count
    wa_sources = Counter()
    top_searches = Counter()
    utm_breakdown = defaultdict(lambda: {"sessions": set(), "wa_clicks": 0, "leads": 0})

    for e in events:
        v = e.get("variant") or "—"
        sid = e.get("session_id")
        et = e.get("event_type", "")
        if sid:
            sessions_by_variant[v].add(sid)
        step_counts[v][et] += 1

        if et == "whatsapp_click":
            md = e.get("metadata") or {}
            src = md.get("source") if isinstance(md, dict) else None
            wa_sources[src or "unknown"] += 1

        if et == "search" and e.get("modelo"):
            top_searches[e["modelo"]] += 1

        utm = e.get("utm_campaign") or e.get("utm_source") or "(direct)"
        if sid:
            utm_breakdown[utm]["sessions"].add(sid)
        if et == "whatsapp_click":
            utm_breakdown[utm]["wa_clicks"] += 1

    for l in leads:
        utm = l.get("utm_campaign") or l.get("utm_source") or "(direct)"
        utm_breakdown[utm]["leads"] += 1

    # Build per-variant funnel rows for the drop-off chart
    funnel_steps = []
    for v in sorted(sessions_by_variant.keys()):
        sess = len(sessions_by_variant[v])
        pv = step_counts[v].get("pageview", 0)
        srch = step_counts[v].get("search", 0)
        cart = step_counts[v].get("add_to_cart", 0)
        wac = step_counts[v].get("whatsapp_click", 0)
        # Leads attributed to this variant via the leads table
        leads_v = sum(1 for l in leads if (l.get("variant") or "—") == v)
        funnel_steps.append({
            "variant": v,
            "sessions": sess,
            "pageviews": pv,
            "searches": srch,
            "carts": cart,
            "wa_clicks": wac,
            "leads": leads_v,
        })

    utm_rows = []
    for k, val in sorted(utm_breakdown.items(), key=lambda kv: -len(kv[1]["sessions"])):
        s = len(val["sessions"])
        if s == 0 and val["wa_clicks"] == 0 and val["leads"] == 0:
            continue
        utm_rows.append({
            "campaign": k,
            "sessions": s,
            "wa_clicks": val["wa_clicks"],
            "leads": val["leads"],
            "wa_pct": round(100 * val["wa_clicks"] / s, 1) if s else 0,
        })

    return {
        "funnel_steps": funnel_steps,
        "wa_sources": wa_sources.most_common(8),
        "top_searches": top_searches.most_common(10),
        "utm_rows": utm_rows[:10],
    }


@app.get("/admin/funnel", response_class=HTMLResponse)
async def admin_funnel(request: Request, days: int = 14, _user: str = Depends(require_admin)):
    """A/B test funnel readout — variant comparison + drop-off + UTM + sources."""
    try:
        summary = await _admin_supabase_rpc("landing_funnel_summary", {"p_days": days})
    except HTTPException:
        summary = []

    # Pull all events + leads in the window (one round-trip each via service_role)
    cutoff = f"now() - interval '{days} days'"  # not interpolated into URL — use server-side timestamp instead
    # PostgREST: filter created_at >= some ISO timestamp. Compute it in Python.
    from datetime import datetime, timedelta, timezone
    since_iso = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    events, leads_in_window, all_leads = await asyncio.gather(
        _admin_supabase_get(
            "landing_events",
            params={
                "select": "created_at,session_id,variant,event_type,modelo,utm_source,utm_campaign,metadata,wa_phone",
                "order": "created_at.desc",
                "created_at": f"gte.{since_iso}",
            },
            range_header="0-9999",
        ),
        _admin_supabase_get(
            "landing_leads",
            params={
                "select": "id,created_at,variant,name,phone,email,customer_type,source,utm_source,utm_campaign",
                "order": "created_at.desc",
                "created_at": f"gte.{since_iso}",
            },
            range_header="0-499",
        ),
        _admin_supabase_get(
            "landing_leads",
            params={"select": "id"},
            range_header="0-9999",
        ),
    )

    breakdowns = _aggregate_funnel(events, leads_in_window)

    return templates.TemplateResponse(
        request=request,
        name="admin_funnel.html",
        context={
            "summary": summary,
            "days": days,
            "total_leads": len(all_leads),
            "leads_in_window": leads_in_window[:20],
            "events_total": len(events),
            "recent_events": events[:80],
            **breakdowns,
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
