-- Landing Page — cart identity column.
--
-- Why: the lead-capture form (bounce modal, cart-save form, mayoreo form)
-- collects email + phone, but landing_carts only had wa_phone. This means
-- a returning visitor whose cart we have on file but who left only an
-- email couldn't be recognized. Adding `email` makes landing_carts the
-- canonical "what do we know about this session" row, so the bounce
-- modal can pre-fill on return and skip if we already have everything.
--
-- Run order: idempotent — safe to re-run. No data is destroyed.

alter table landing_carts
    add column if not exists email text;

create index if not exists idx_landing_carts_email
    on landing_carts (email);
