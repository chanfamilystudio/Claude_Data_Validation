-- Drops everything synthetic.sql / synthetic_clean.sql create. Safe to run when nothing exists.
DROP TABLE IF EXISTS stg_wd.workers;
DROP TABLE IF EXISTS stg_wd.payroll_journal;
DROP TABLE IF EXISTS stg_gp.UPR00100;
DROP TABLE IF EXISTS stg_gp.UPR40300;
DROP TABLE IF EXISTS stg_gp.GL20000;
DROP TABLE IF EXISTS xref.cost_center_map;
DROP TABLE IF EXISTS xref.account_map;
GO
DROP SCHEMA IF EXISTS stg_wd;
GO
DROP SCHEMA IF EXISTS stg_gp;
GO
DROP SCHEMA IF EXISTS xref;
GO
