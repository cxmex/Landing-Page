-- Landing Page — bestsellers materialized view.
--
-- Why: /api/bestsellers used to call get_order_analysis() + join 3 tables on
-- every request, which exceeds Supabase's default 8s statement_timeout for the
-- anon role and returns 500. This MV pre-computes the join during a scheduled
-- refresh (where we raise the timeout locally) so the request path is just a
-- single indexed SELECT and returns in <50ms.
--
-- Run order in Supabase SQL editor:
--   1. Enable pg_cron extension once (Database → Extensions → enable "pg_cron")
--   2. Run this whole file
--   3. Manually call refresh_landing_bestsellers() once to populate
--   4. Confirm the cron job exists with: select * from cron.job;

-- ─────────────────────────────────────────────────────────────────────────────
-- 1. The materialized view
-- ─────────────────────────────────────────────────────────────────────────────
-- One row per estilo (the top-selling color/modelo for that style), already
-- joined to its primary image. Filtered to in-stock items only — anything
-- shown on the landing page must be sellable today.
drop materialized view if exists landing_bestsellers cascade;

create materialized view landing_bestsellers as
with
    -- Last 30 days of order analysis. days_back arg matches the value the app
    -- used to pass; if the inventory team ever wants a different window, just
    -- redeploy the MV with the new constant.
    base as (
        select
            (a.estilo)::text  as estilo,
            (a.color)::text   as color,
            (a.modelo)::text  as modelo,
            (a.sold_total)::numeric  as sold_total,
            (a.stock_total)::numeric as stock_total
        from get_order_analysis(30) a
        where coalesce((a.sold_total)::numeric, 0) > 0
          and coalesce((a.stock_total)::numeric, 0) > 0
          and (a.estilo) is not null
    ),
    with_ids as (
        select
            b.*,
            e.id as estilo_id,
            c.id as color_id
        from base b
        left join inventario_estilos e
               on e.nombre = b.estilo
        left join inventario_colores c
               on upper(trim(c.color)) = upper(trim(b.color))
    ),
    -- Pick the image that matches the (estilo, color) where possible, else any
    -- image for that estilo. Most recent upload wins. (When the inventory app's
    -- migrations_image_order.sql is applied, swap to ordering by display_order.)
    with_image as (
        select
            wi.*,
            coalesce(
                (select iu.public_url
                   from image_uploads iu
                  where iu.estilo_id = wi.estilo_id
                    and iu.color_id  = wi.color_id
                  order by iu.created_at desc
                  limit 1),
                (select iu.public_url
                   from image_uploads iu
                  where iu.estilo_id = wi.estilo_id
                  order by iu.created_at desc
                  limit 1)
            ) as image_url
        from with_ids wi
    ),
    -- One row per estilo: the top-selling color/modelo for that style.
    ranked as (
        select
            estilo,
            color,
            modelo,
            sold_total::int  as sold,
            stock_total::int as stock,
            image_url,
            row_number() over (
                partition by estilo
                order by sold_total desc, stock_total desc
            ) as rn
        from with_image
        where image_url is not null and image_url <> ''
    )
select
    estilo,
    color,
    modelo,
    sold,
    stock,
    image_url
from ranked
where rn = 1
order by sold desc;

-- Unique index is required for REFRESH MATERIALIZED VIEW CONCURRENTLY.
create unique index if not exists idx_landing_bestsellers_estilo
    on landing_bestsellers (estilo);

-- Sort/limit index for the typical query (top N by sold).
create index if not exists idx_landing_bestsellers_sold
    on landing_bestsellers (sold desc);

-- ─────────────────────────────────────────────────────────────────────────────
-- 2. Refresh wrapper — raises statement_timeout locally so the heavy
--    get_order_analysis() call doesn't get killed during refresh.
-- ─────────────────────────────────────────────────────────────────────────────
create or replace function refresh_landing_bestsellers()
returns void
language plpgsql
security definer
as $$
begin
    -- 5 minutes is plenty even with a few thousand SKUs; tune up if needed.
    set local statement_timeout = '300s';
    -- CONCURRENTLY = readers don't see an empty MV mid-refresh.
    refresh materialized view concurrently landing_bestsellers;
end;
$$;

-- ─────────────────────────────────────────────────────────────────────────────
-- 3. Permissions — anon (used by the FastAPI server) can SELECT the MV.
-- ─────────────────────────────────────────────────────────────────────────────
grant select on landing_bestsellers to anon, authenticated;

-- ─────────────────────────────────────────────────────────────────────────────
-- 4. Schedule the refresh every 30 minutes via pg_cron.
--    If pg_cron isn't enabled yet, enable it once in Database → Extensions
--    and re-run this block.
-- ─────────────────────────────────────────────────────────────────────────────
do $$
begin
    if exists (select 1 from pg_extension where extname = 'pg_cron') then
        -- Remove any prior schedule with the same name (re-run safe).
        perform cron.unschedule(jobid)
        from cron.job
        where jobname = 'refresh_landing_bestsellers';

        perform cron.schedule(
            'refresh_landing_bestsellers',
            '*/30 * * * *',
            $cron$ select refresh_landing_bestsellers(); $cron$
        );
    else
        raise notice 'pg_cron not enabled — skipping schedule. Enable in Database → Extensions, then re-run this DO block.';
    end if;
end
$$;

-- ─────────────────────────────────────────────────────────────────────────────
-- 5. First population (run after the schedule is set so the page works
--    immediately instead of waiting up to 30 min for the first cron tick).
-- ─────────────────────────────────────────────────────────────────────────────
select refresh_landing_bestsellers();
