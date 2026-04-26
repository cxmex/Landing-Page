-- Landing Page schema. Run once in the Supabase SQL editor.
-- These tables are owned by the landing page app and are separate from
-- the inventory app's tables (inventario, inventario_modelos, inventario_estilos).

create table if not exists landing_events (
    id              bigserial primary key,
    created_at      timestamptz not null default now(),
    session_id      text not null,
    variant         text,                       -- A | B | C
    event_type      text not null,              -- pageview | click | add_to_cart | remove_from_cart | whatsapp_click | call_click | search | lead_submit | bounce
    modelo          text,
    estilo          text,
    color           text,
    barcode         text,
    quantity        integer,
    wa_phone        text,
    utm_source      text,
    utm_campaign    text,
    utm_medium      text,
    user_agent      text,
    referrer        text,
    metadata        jsonb
);
create index if not exists idx_landing_events_session on landing_events (session_id);
create index if not exists idx_landing_events_variant on landing_events (variant, event_type);
create index if not exists idx_landing_events_created on landing_events (created_at desc);

create table if not exists landing_leads (
    id              bigserial primary key,
    created_at      timestamptz not null default now(),
    session_id      text,
    variant         text,
    email           text,
    phone           text,
    name            text,
    customer_type   text,                       -- retail | wholesale | unknown
    source          text,                       -- bounce_modal | inline_form | whatsapp_redirect
    utm_source      text,
    utm_campaign    text,
    metadata        jsonb
);
create index if not exists idx_landing_leads_email on landing_leads (email);
create index if not exists idx_landing_leads_phone on landing_leads (phone);
create index if not exists idx_landing_leads_created on landing_leads (created_at desc);

create table if not exists landing_carts (
    id              bigserial primary key,
    created_at      timestamptz not null default now(),
    updated_at      timestamptz not null default now(),
    session_id      text not null unique,
    wa_phone        text,
    variant         text,
    items           jsonb not null default '[]'::jsonb,
    -- items shape: [{barcode, modelo, estilo, color, qty, unit_price}]
    customer_type   text,                       -- retail | wholesale | unknown
    status          text not null default 'open' -- open | sent_to_whatsapp | abandoned | converted
);
create index if not exists idx_landing_carts_wa on landing_carts (wa_phone);
create index if not exists idx_landing_carts_updated on landing_carts (updated_at desc);

-- Convenience RPC: aggregate funnel metrics by variant for the last N days.
create or replace function landing_funnel_summary(p_days integer default 14)
returns table (
    variant         text,
    sessions        bigint,
    pageviews       bigint,
    add_to_carts    bigint,
    whatsapp_clicks bigint,
    leads           bigint
) language sql stable as $$
    select
        e.variant,
        count(distinct e.session_id) as sessions,
        count(*) filter (where e.event_type = 'pageview') as pageviews,
        count(*) filter (where e.event_type = 'add_to_cart') as add_to_carts,
        count(*) filter (where e.event_type = 'whatsapp_click') as whatsapp_clicks,
        (select count(*) from landing_leads l
            where l.variant = e.variant
              and l.created_at >= now() - (p_days || ' days')::interval) as leads
    from landing_events e
    where e.created_at >= now() - (p_days || ' days')::interval
      and e.variant is not null
    group by e.variant
    order by e.variant;
$$;

-- ─────────────────────────────────────────────────────────────────────────────
-- Row Level Security
-- ─────────────────────────────────────────────────────────────────────────────
-- Threat model: in this app the anon key is held by the FastAPI server only;
-- the browser does NOT call Supabase directly (everything goes through /api/*).
-- We still enable RLS so that:
--   * if the anon key ever leaks, a stranger can write garbage events/leads
--     but cannot READ existing leads or events;
--   * the admin dashboard reads via SUPABASE_SERVICE_KEY (set in env) which
--     bypasses RLS, so admin queries continue to work.
-- ─────────────────────────────────────────────────────────────────────────────

alter table landing_events enable row level security;
alter table landing_leads  enable row level security;
alter table landing_carts  enable row level security;

-- Drop existing policies if re-running this script (covers both old and new names).
drop policy if exists "anon_insert_events" on landing_events;
drop policy if exists "anon_insert_leads"  on landing_leads;
drop policy if exists "anon_carts_rw"      on landing_carts;
drop policy if exists "lp_insert_events"   on landing_events;
drop policy if exists "lp_insert_leads"    on landing_leads;
drop policy if exists "lp_carts_rw"        on landing_carts;

-- INSERT only on events + leads, full RW on carts. Use `to public` so the policy
-- matches any role PostgREST authenticates as (anon JWT, authenticated, etc.) —
-- `to anon` was failing in practice on this Supabase project. RLS still denies
-- SELECT/UPDATE/DELETE on events+leads to non-service-role users (no policies).
create policy "lp_insert_events" on landing_events
    for insert to public with check (true);

create policy "lp_insert_leads" on landing_leads
    for insert to public with check (true);

create policy "lp_carts_rw" on landing_carts
    for all to public using (true) with check (true);

-- service_role bypasses RLS automatically — admin dashboard SELECTs work via
-- SUPABASE_SERVICE_KEY in the FastAPI env (added separately to .env).

