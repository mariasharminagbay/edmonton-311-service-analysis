-- =====================================================================
-- sql/03_marts.sql
-- Small summary tables, one per requirement, built from staging.requests.
-- The dashboard and the model read these, not staging.
--
-- How to run: in pgAdmin, open a Query Tool on edmonton_311,
-- File > Open this file, then press F5. Expect a minute or two.
-- Safe to re-run: it drops and rebuilds every table it creates.
--
-- Rules carried over from staging:
--   * Turnaround and "late" use closed Referral requests outside the
--     instant-close categories (rows where is_late is not null).
--   * First-contact resolution = Full Service / (Full Service + Referral);
--     rows with an Unknown referral type are left out of it.
--   * Comparing years: use the *_mature late rates. A request only counts
--     once it is older than its category's target, so it has had the full
--     time to be late; open requests past target count as late. Without
--     this, slow categories show near 0% late in 2026 only because their
--     2026 requests have not existed long enough (e.g. Permits - Building).
-- =====================================================================

begin;

create schema if not exists marts;


-- ---------------------------------------------------------------------
-- R1, R2: monthly volume, channel and first-contact resolution
-- ---------------------------------------------------------------------
drop table if exists marts.monthly_volume;
create table marts.monthly_volume as
select
    date_trunc('month', created_at)::date                     as created_month,
    service_category,
    interaction_channel,
    count(*)                                                  as requests,
    count(*) filter (where referral_type = 'Full Service')    as full_service,
    count(*) filter (where referral_type = 'Referral')        as referrals,
    count(*) filter (where referral_type = 'Unknown')         as unknown_type
from staging.requests
group by 1, 2, 3;


-- ---------------------------------------------------------------------
-- R3: turnaround and late rate by category
-- ---------------------------------------------------------------------
drop table if exists marts.request_type_performance;
create table marts.request_type_performance as
with data_end as (
    select max(created_at) as max_created from staging.requests
),
r as (
    select s.*,
           s.referral_type = 'Referral' and not s.is_instant_category
             and (s.is_late is not null or s.is_open)
             and extract(epoch from (d.max_created - s.created_at)) / 86400.0 > s.target_days
                                                              as is_mature,
           coalesce(s.is_late, s.is_overdue)                  as late_known
    from staging.requests s
    cross join data_end d
)
select
    service_category,
    bool_or(is_instant_category)                              as is_instant_category,
    max(target_days)                                          as target_days,
    count(*)                                                  as requests,
    count(*) filter (where is_late is not null)               as closed_referrals,
    percentile_cont(0.5) within group (order by days_to_close)
        filter (where is_late is not null)                    as median_days_to_close,
    percentile_cont(0.9) within group (order by days_to_close)
        filter (where is_late is not null)                    as p90_days_to_close,
    avg(is_late::int) filter (where is_late is not null and is_training_period)
                                                              as pct_late_2023_2025,
    avg(is_late::int) filter (where is_late is not null and not is_training_period)
                                                              as pct_late_2026,
    count(*) filter (where is_open)                           as open_requests,
    count(*) filter (where is_overdue)                        as overdue_requests,
    avg(late_known::int) filter (where is_mature and is_training_period)
                                                              as pct_late_2023_2025_mature,
    avg(late_known::int) filter (where is_mature and not is_training_period)
                                                              as pct_late_2026_mature,
    count(*) filter (where is_mature and not is_training_period)
                                                              as mature_2026_referrals
from r
group by service_category;


-- ---------------------------------------------------------------------
-- R4: open backlog by age
-- ---------------------------------------------------------------------
drop table if exists marts.backlog_age;
create table marts.backlog_age as
select
    service_category,
    age_bucket,
    count(*)                                                  as open_requests,
    count(*) filter (where is_overdue)                        as overdue_requests,
    count(*) filter (where is_likely_stale)                   as likely_stale
from staging.requests
where is_open
group by 1, 2;


-- ---------------------------------------------------------------------
-- R5: ward comparison
-- Compare wards on late rate, not median days: the late flag is set
-- per category, so it is fair to wards with different request mixes.
-- ---------------------------------------------------------------------
drop table if exists marts.ward_summary;
create table marts.ward_summary as
with data_end as (
    select max(created_at) as max_created from staging.requests
),
r as (
    select s.*,
           s.referral_type = 'Referral' and not s.is_instant_category
             and (s.is_late is not null or s.is_open)
             and extract(epoch from (d.max_created - s.created_at)) / 86400.0 > s.target_days
                                                              as is_mature,
           coalesce(s.is_late, s.is_overdue)                  as late_known
    from staging.requests s
    cross join data_end d
)
select
    coalesce(ward, 'Unknown')                                 as ward,
    count(*)                                                  as requests,
    count(*) filter (where is_late is not null)               as closed_referrals,
    percentile_cont(0.5) within group (order by days_to_close)
        filter (where is_late is not null)                    as median_days_to_close,
    avg(is_late::int) filter (where is_late is not null and is_training_period)
                                                              as pct_late_2023_2025,
    avg(is_late::int) filter (where is_late is not null and not is_training_period)
                                                              as pct_late_2026,
    count(*) filter (where is_open)                           as open_requests,
    count(*) filter (where is_overdue)                        as overdue_requests,
    avg(late_known::int) filter (where is_mature and is_training_period)
                                                              as pct_late_2023_2025_mature,
    avg(late_known::int) filter (where is_mature and not is_training_period)
                                                              as pct_late_2026_mature,
    count(*) filter (where is_mature and not is_training_period)
                                                              as mature_2026_referrals
from r
group by 1;


-- ---------------------------------------------------------------------
-- R6: model data
-- Label = late. Closed referrals have a known outcome. Open referrals
-- already past their target are late too, whatever happens next, so
-- they are kept; this reduces the bias from slow requests still open.
-- Open referrals not yet past target have no known outcome and are left
-- out. days_before_data_end lets Phase 6 test only on requests old
-- enough to have had time to close.
-- ---------------------------------------------------------------------
drop table if exists marts.model_training;
create table marts.model_training as
with data_end as (
    select max(created_at) as max_created from staging.requests
)
select
    r.row_id,
    r.service_category,
    r.service_description,
    r.service_area,
    r.interaction_channel,
    coalesce(r.ward, 'Unknown')                               as ward,
    coalesce(r.neighbourhood, 'Unknown')                      as neighbourhood,
    extract(month from r.created_at)::int                     as created_month_num,
    extract(dow   from r.created_at)::int                     as created_weekday,   -- 0 = Sunday
    extract(hour  from r.created_at)::int                     as created_hour,
    r.created_year,
    r.is_training_period,
    r.target_days,
    r.is_open,
    extract(epoch from (d.max_created - r.created_at)) / 86400.0
                                                              as days_before_data_end,
    coalesce(r.is_late, r.is_overdue)::int                    as is_late   -- the label
from staging.requests r
cross join data_end d
where r.is_late is not null
   or r.is_overdue;

alter table marts.model_training add primary key (row_id);

commit;


-- =====================================================================
-- Checks: run these one at a time afterwards (highlight, then F5)
-- =====================================================================

-- Every request counted once in monthly volume (should equal 3,446,386)
-- select sum(requests) from marts.monthly_volume;

-- First-contact resolution by year
-- select extract(year from created_month)::int as year,
--        round(100.0 * sum(full_service) / nullif(sum(full_service) + sum(referrals), 0), 1) as fcr_pct
-- from marts.monthly_volume
-- group by 1 order by 1;

-- Slowest categories with enough volume
-- select service_category, closed_referrals,
--        round(median_days_to_close::numeric, 1) as median_days,
--        round(100 * pct_late_2023_2025_mature, 1) as late_2023_2025,
--        round(100 * pct_late_2026_mature, 1)      as late_2026,
--        mature_2026_referrals
-- from marts.request_type_performance
-- where closed_referrals >= 1000
-- order by median_days_to_close desc
-- limit 10;

-- Ward comparison
-- select ward, closed_referrals,
--        round(100 * pct_late_2023_2025_mature, 1) as late_2023_2025,
--        round(100 * pct_late_2026_mature, 1)      as late_2026,
--        mature_2026_referrals, open_requests
-- from marts.ward_summary
-- order by pct_late_2026_mature desc;

-- Model data: size and late share by period
-- select is_training_period, is_open, count(*), round(100.0 * avg(is_late), 1) as pct_late
-- from marts.model_training
-- group by 1, 2 order by 1 desc, 2;
