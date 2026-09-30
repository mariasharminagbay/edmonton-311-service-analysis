-- =====================================================================
-- sql/02_staging.sql
-- Builds the cleaned staging layer from raw.requests.
--
-- How to run: in pgAdmin, open a Query Tool on edmonton_311,
-- File > Open this file, then press F5. Expect a few minutes.
-- Safe to re-run: it drops and rebuilds the tables it creates.
--
-- Creates:
--   staging.requests          one typed, cleaned row per 311 request
--   staging.category_targets  the "late" threshold for each category
--
-- Definition of late (decided from the data, Sep 2026):
--   A closed Referral request is late if it took longer than the
--   75th percentile of its own service category. Thresholds come from
--   requests created 2023-2025 only (training period), then apply to
--   every year. Categories with fewer than 500 training requests use
--   the overall 75th percentile instead.
--
-- Instant-close categories: where at least 75% of the 2023-2025
-- referrals closed within 15 minutes (e.g. Lost and Found), close times
-- record admin, not service work. They get no late flag and are left
-- out of the model.
-- =====================================================================

begin;

create schema if not exists staging;

drop table if exists staging.requests;
drop table if exists staging.category_targets;
drop table if exists staging.requests_base;


-- ---------------------------------------------------------------------
-- 1. Typed, cleaned copy of every request
-- ---------------------------------------------------------------------
create table staging.requests_base as
with typed as (
    select
        row_id::text                                               as row_id,
        nullif(trim(date_created::text), '')::timestamp                  as created_at,
        nullif(trim(date_closed::text),  '')::timestamp                  as closed_at,
        trim(request_status::text)                                       as request_status,
        coalesce(nullif(trim(referral_type::text), ''), 'Unknown')       as referral_type,
        -- ::text guards against columns pandas typed as numbers
        -- the source has double spaces inside names; collapse them
        coalesce(regexp_replace(trim(service_category::text),    '\s+', ' ', 'g'), 'Unknown') as service_category,
        coalesce(regexp_replace(trim(service_description::text), '\s+', ' ', 'g'), 'Unknown') as service_description,
        coalesce(regexp_replace(trim(service_area::text),        '\s+', ' ', 'g'), 'Unknown') as service_area,
        coalesce(nullif(trim(interaction_channel::text), ''), 'Unknown') as interaction_channel,
        nullif(trim(status_detail::text), '')                            as status_detail,
        nullif(trim(neighbourhood_id::text), '')                         as neighbourhood_id,
        nullif(trim(neighbourhood::text), '')                            as neighbourhood,
        nullif(trim(ward::text), '')                                     as ward,
        -- area centre points only (the City does not publish exact locations)
        nullif(trim(nbhd_latitude::text),  '')::numeric                  as nbhd_latitude,
        nullif(trim(nbhd_longitude::text), '')::numeric                  as nbhd_longitude,
        nullif(trim(ward_latitude::text),  '')::numeric                  as ward_latitude,
        nullif(trim(ward_longitude::text), '')::numeric                  as ward_longitude
    from raw.requests
)
select
    t.*,
    t.request_status = 'Open'                                      as is_open,
    extract(epoch from (t.closed_at - t.created_at)) / 3600.0      as hours_to_close,
    extract(epoch from (t.closed_at - t.created_at)) / 86400.0     as days_to_close,
    -- age of open requests, measured to the newest request in the data
    -- (not today's date, so re-running later doesn't change the answer)
    case when t.request_status = 'Open'
         then extract(epoch from ((select max(created_at) from typed) - t.created_at)) / 86400.0
    end                                                            as age_days
from typed t;


-- ---------------------------------------------------------------------
-- 2. Late threshold per category, from the training period only
-- ---------------------------------------------------------------------
create table staging.category_targets as
with train as (
    select service_category, days_to_close, hours_to_close
    from staging.requests_base
    where referral_type = 'Referral'
      and not is_open
      and days_to_close >= 0
      and created_at < date '2026-01-01'
),
overall as (
    select percentile_cont(0.75) within group (order by days_to_close) as p75
    from train
),
by_category as (
    select service_category,
           count(*)                                                  as train_requests,
           percentile_cont(0.75) within group (order by days_to_close) as p75,
           avg((hours_to_close < 0.25)::int)                           as pct_closed_15min
    from train
    group by service_category
)
select
    c.service_category,
    c.train_requests,
    c.p75                                                  as category_p75_days,
    o.p75                                                  as overall_p75_days,
    case when c.train_requests >= 500 then c.p75 else o.p75 end as target_days,
    c.train_requests >= 500                                as uses_own_target,
    round(c.pct_closed_15min, 3)                           as pct_closed_15min,
    c.pct_closed_15min >= 0.75                             as is_instant_category
from by_category c
cross join overall o;


-- ---------------------------------------------------------------------
-- 3. Final staging table: base + targets + flags
-- ---------------------------------------------------------------------
create table staging.requests as
with fallback as (
    select max(overall_p75_days) as p75 from staging.category_targets
),
joined as (
    select b.*,
           coalesce(t.target_days, f.p75)          as target_days,
           coalesce(t.is_instant_category, false) as is_instant_category
    from staging.requests_base b
    left join staging.category_targets t using (service_category)
    cross join fallback f
)
select
    j.*,
    extract(year from j.created_at)::int                  as created_year,
    j.created_at < date '2026-01-01'                      as is_training_period,

    -- outcome for the model: closed Referral requests only,
    -- outside instant-close categories
    case when j.referral_type = 'Referral' and not j.is_open and j.days_to_close >= 0
              and not j.is_instant_category
         then j.days_to_close > j.target_days
    end                                                   as is_late,

    -- open Referral requests already past their category's target
    case when j.referral_type = 'Referral' and j.is_open
              and not j.is_instant_category
         then j.age_days > j.target_days
    end                                                   as is_overdue,

    case when j.is_open then
         case when j.age_days <= 7   then '0-7 days'
              when j.age_days <= 30  then '8-30 days'
              when j.age_days <= 90  then '31-90 days'
              when j.age_days <= 365 then '91-365 days'
              else '365+ days'
         end
    end                                                   as age_bucket,

    coalesce(j.is_open and j.age_days > 365, false)       as is_likely_stale,

    -- data quality flags (kept, not dropped, so they can be reported)
    (not j.is_open and j.closed_at is null)               as dq_closed_no_date,
    (j.is_open and j.referral_type = 'Full Service')      as dq_open_full_service,
    (j.referral_type = 'Unknown')                         as dq_missing_referral_type,
    coalesce(j.days_to_close < 0, false)                  as dq_negative_duration
from joined j;

alter table staging.requests add primary key (row_id);
create index on staging.requests (created_at);
create index on staging.requests (service_category);

drop table staging.requests_base;

commit;


-- =====================================================================
-- Checks: run these one at a time afterwards (highlight, then F5)
-- =====================================================================

-- Row count must equal raw.requests (3,446,386 at first load)
-- select (select count(*) from raw.requests)     as raw_rows,
--        (select count(*) from staging.requests) as staging_rows;

-- Late share by category: each should sit near 25% in the training years
-- select service_category, is_training_period,
--        count(*) filter (where is_late is not null) as closed_referrals,
--        round(100.0 * avg(is_late::int), 1)        as pct_late
-- from staging.requests
-- where is_late is not null
-- group by service_category, is_training_period
-- order by closed_referrals desc
-- limit 20;

-- Data quality summary
-- select count(*) filter (where dq_closed_no_date)        as closed_no_date,
--        count(*) filter (where dq_open_full_service)     as open_full_service,
--        count(*) filter (where dq_missing_referral_type) as missing_referral_type,
--        count(*) filter (where dq_negative_duration)     as negative_duration,
--        count(*) filter (where is_likely_stale)          as likely_stale_open
-- from staging.requests;

-- The thresholds themselves
-- select * from staging.category_targets order by train_requests desc;

-- Which categories were flagged instant-close, and how many requests
-- select service_category, train_requests, pct_closed_15min
-- from staging.category_targets
-- where is_instant_category
-- order by train_requests desc;
