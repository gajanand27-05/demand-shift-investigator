-- Register the local Postgres (container taxi-pg on the zetaris_default network) as a Zetaris datasource.
-- Run one statement at a time (data/setup/register_zetaris.py does this).
CREATE DATASOURCE nyc_taxi_pg DESCRIBE BY "Real NYC TLC trip sample, taxi zones, local Postgres" OPTIONS (jdbcdriver "org.postgresql.Driver", jdbcurl "jdbc:postgresql://taxi-pg:5432/taxi", username "taxi", password "taxi_local_only", fetchsize "5000")
REGISTER DATASOURCE TABLES FROM nyc_taxi_pg
