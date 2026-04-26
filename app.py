"""
Landing Page — Phone Cases (Mexico).

Standalone FastAPI app that reads the same Supabase project as the inventory app
(`cxmex/inventoriorapido1`). Owns its own A/B testing, event logging, and cart tables.
"""

import os
import json
import random
import secrets
import asyncio
from typing import Optional

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Request, HTTPException, Form, Query
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

load_dotenv()

SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://gbkhkbfbarsnpbdkxzii.supabase.co")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
WHATSAPP_NUMBER = os.environ.get("WHATSAPP_NUMBER", "525545174085")
WHATSAPP_DEFAULT_MESSAGE = os.environ.get(
    "WHATSAPP_DEFAULT_MESSAGE",
    "Hola, vi su catalogo de fundas y me interesa",
)
BUSINESS_PHONE = os.environ.get("BUSINESS_PHONE", "525545174085")

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

app = FastAPI(title="Phone Cases — Landing Page")
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


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


# ──────────────────────────────────────────────────────────────────────────────
# Catalog data (ported from inventory app's /api/browse-modelo)
# ──────────────────────────────────────────────────────────────────────────────

async def fetch_modelos():
    return await supabase_get(
        "inventario_modelos",
        params={"select": "id,modelo,marca", "order": "modelo.asc"},
    )


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
    qs_str = ("?" + "&".join(f"{k}={v}" for k, v in qs.items())) if qs else ""

    response = RedirectResponse(url=f"/v/{variant}{qs_str}", status_code=302)
    response.set_cookie(SESSION_COOKIE, session_id, max_age=60 * 60 * 24 * 90, httponly=True, samesite="lax")
    response.set_cookie(VARIANT_COOKIE, variant, max_age=60 * 60 * 24 * 30, samesite="lax")
    return response


@app.get("/v/{variant}", response_class=HTMLResponse)
async def variant_page(request: Request, variant: str):
    variant = variant.upper()
    if variant not in VARIANTS:
        raise HTTPException(status_code=404, detail="Unknown variant")

    session_id = get_or_create_session_id(request)
    modelos = await fetch_modelos()

    await log_event(request, session_id, variant, "pageview")

    template_name = f"variant_{variant.lower()}.html"
    response = templates.TemplateResponse(
        request=request,
        name=template_name,
        context={
            "modelos": modelos,
            "variant": variant,
            "wa_number": WHATSAPP_NUMBER,
            "wa_default_msg": WHATSAPP_DEFAULT_MESSAGE,
            "business_phone": BUSINESS_PHONE,
            "session_id": session_id,
        },
    )
    response.set_cookie(SESSION_COOKIE, session_id, max_age=60 * 60 * 24 * 90, httponly=True, samesite="lax")
    response.set_cookie(VARIANT_COOKIE, variant, max_age=60 * 60 * 24 * 30, samesite="lax")
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
    }
    await supabase_post("landing_leads", [payload])
    await log_event(request, session_id, variant, "lead_submit", metadata={"source": source})
    return {"ok": True}


@app.get("/healthz")
async def healthz():
    return {"ok": True}
