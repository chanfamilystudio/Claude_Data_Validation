-- Companion to synthetic.sql: the same tables with data that reconciles. `recon run --all --period 2026-09`
-- must find zero breaks. Idempotent: drops and re-creates everything.
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
CREATE SCHEMA stg_wd;
GO
CREATE SCHEMA stg_gp;
GO
CREATE SCHEMA xref;
GO
CREATE TABLE stg_wd.workers (
    employee_id   VARCHAR(20),
    cost_center   VARCHAR(20),
    hire_date     DATE,
    annual_salary DECIMAL(18,2),
    national_id   VARCHAR(20)
);
CREATE TABLE stg_gp.UPR00100 (
    EMPLOYID CHAR(15),
    DEPRTMNT CHAR(6),
    STRTDATE DATETIME,
    SALARY   NUMERIC(19,5),
    SOCSCNUM CHAR(15),
    INACTIVE TINYINT
);
CREATE TABLE stg_gp.UPR40300 (
    DEPRTMNT CHAR(6),
    DSCRIPTN VARCHAR(30)
);
CREATE TABLE xref.cost_center_map (
    wd_cost_center VARCHAR(20),
    gp_dept        VARCHAR(6),
    eff_start      DATE,
    eff_end        DATE NULL
);
CREATE TABLE xref.account_map (
    wd_account VARCHAR(20),
    gp_actindx INT
);
CREATE TABLE stg_wd.payroll_journal (
    ledger_account VARCHAR(20),
    debit          DECIMAL(18,2),
    credit         DECIMAL(18,2),
    pay_date       DATE,
    load_ts        DATETIME2
);
CREATE TABLE stg_gp.GL20000 (
    ACTINDX   INT,
    DEBITAMT  NUMERIC(19,5),
    CRDTAMNT  NUMERIC(19,5),
    TRXDATE   DATETIME,
    SOURCDOC  CHAR(11)
);
GO
INSERT INTO stg_gp.UPR40300 VALUES ('D1', 'One'), ('D2', 'Two'), ('D5', 'Five');
GO
INSERT INTO xref.cost_center_map VALUES
    ('CC1', 'D1', '2000-01-01', NULL),
    ('CC2', 'D2', '2000-01-01', NULL),
    ('CC4', 'D4', '2000-01-01', '2025-12-31'),
    ('CC4', 'D5', '2026-01-01', NULL);
GO
INSERT INTO xref.account_map VALUES ('A6000', 100), ('A6100', 101);
GO
INSERT INTO stg_wd.workers VALUES
    ('000001',  'CC1',  '2020-01-01', 50000.00, '111-11-1111'),
    ('000002 ', 'cc2 ', '2021-02-01', 60000.00, '222-22-2222'),
    ('000003',  'CC4',  '2022-03-01', 70000.00, '333-33-3333');
GO
INSERT INTO stg_gp.UPR00100 VALUES
    ('000001', 'D1', '2020-01-01', 50000,      '111-11-1111', 0),
    ('000002', 'D2', '2021-02-01', 60000.004,  '222-22-2222', 0),
    ('000003', 'D5', '2022-03-01', 70000,      '333-33-3333', 1);
GO
INSERT INTO stg_wd.payroll_journal VALUES
    ('A6000', 1000.00, 0, '2026-09-15', SYSDATETIME()),
    ('A6100',  500.00, 0, '2026-09-20', SYSDATETIME()),
    ('A6000',  900.00, 0, '2026-10-15', SYSDATETIME());
GO
INSERT INTO stg_gp.GL20000 VALUES
    (100, 1000, 0, '2026-09-30 14:35', 'UPRCC'),
    (101,  500, 0, '2026-09-10',       'UPRCC'),
    (100,    5, 0, '2026-09-10',       'GJ');
GO
