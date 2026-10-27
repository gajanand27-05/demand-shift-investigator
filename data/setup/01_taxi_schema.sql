-- Postgres schema for the real NYC TLC trip sample (from s3://zetaris-public-dataset/ny_taxi/taxi_trips).
-- Loaded by data/setup/load_postgres.sh; Zetaris reads it through CREATE DATASOURCE (see 02_zetaris_register.sql).
DROP TABLE IF EXISTS trips_raw;
CREATE TABLE trips_raw (
  trip_id bigint, trip_type int, vendor_id int,
  pickup_datetime timestamptz, pickup_location_id int,
  dropoff_datetime timestamptz, dropoff_location_id int,
  passenger_count numeric, trip_distance numeric, rate_code_id numeric, store_and_fwd_flag text,
  fare_amount numeric, extra numeric, mta_tax numeric, tip_amount numeric, tolls_amount numeric,
  improvement_surcharge numeric, congestion_surcharge numeric, airport_fee numeric, ehail_fee numeric,
  total_amount numeric, payment_type numeric,
  pickup_hour numeric, pickup_day_of_week numeric, pickup_month numeric, pickup_year numeric,
  pickup_date date, is_weekend boolean, trip_duration_minutes numeric, trip_speed_mph numeric,
  fare_per_mile numeric, tip_percentage numeric, is_valid_trip boolean, created_at timestamptz
);
DROP TABLE IF EXISTS zones;
CREATE TABLE zones (location_id int PRIMARY KEY, zone text, borough text);
