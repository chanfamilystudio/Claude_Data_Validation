-- Synthetic staging data for the fixture project (tests/fixtures/project): Workday on the left (stg_wd),
-- Great Plains on the right (stg_gp), crosswalks in xref. Idempotent: drops and re-creates everything.
-- Run batch by batch (GO separators). Employees are snapshot rules; payroll is periodic and its rows
-- fall in September 2026, so run it with --period 2026-09.
--
-- Planted breaks (rule -> break type -> business key):
--   emp_master_match   missing_right   000006        (Workday only)
--                      missing_left    000007, ABC123 (GP only)
--                      value_mismatch  000003 annual_salary (71000 vs 70000)
--                                      000004 national_id (PII: differs, shown as ***)
--                                      000005 cost_center (effective-dated mapping: CC4 -> D5 since 2026-01-01,
--                                             GP still has the old D4)
--                      mapping_missing 000008        (CC9 has no crosswalk row)
--                      quality_violation 000010      (duplicate key in GP, two rows)
--   emp_cost_center_map mapping_missing CC9; mapping_mismatch CC3 (maps to D9, absent from GP departments)
--   gp_emp_quality     quality_violation: not_null DEPRTMNT (ABC123), unique EMPLOYID (000010),
--                      accepted_values INACTIVE (000011 = 2), references DEPRTMNT (000009 = D9),
--                      like EMPLOYID (ABC123), custom_sql future_hire_dates (000011)
--   pay_jrnl_totals    total_mismatch  A6100 net_amount, A6200 net_amount + line_count, UNMAPPED:A9999
-- Also planted: key whitespace/case (Workday '000002 ' vs GP '000002'), non-UPRCC and out-of-period
-- GP rows, an October Workday row that must not count towards September.

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
INSERT INTO stg_gp.UPR40300 VALUES ('D1', 'One'), ('D2', 'Two'), ('D4', 'Four (old)'), ('D5', 'Five');
GO
-- CC4 changed department on 2026-01-01; CC3 maps to D9, which GP does not have.
INSERT INTO xref.cost_center_map VALUES
    ('CC1', 'D1', '2000-01-01', NULL),
    ('CC2', 'D2', '2000-01-01', NULL),
    ('CC3', 'D9', '2000-01-01', NULL),
    ('CC4', 'D4', '2000-01-01', '2025-12-31'),
    ('CC4', 'D5', '2026-01-01', NULL);
GO
INSERT INTO xref.account_map VALUES ('A6000', 100), ('A6100', 101), ('A6200', 102);
GO
INSERT INTO stg_wd.workers VALUES
    ('000001',  'CC1',  '2020-01-01', 50000.00, '111-11-1111'),
    ('000002 ', 'cc2 ', '2021-02-01', 60000.00, '222-22-2222'),
    ('000003',  'CC1',  '2022-03-01', 70000.00, '333-33-3333'),
    ('000004',  'CC2',  '2022-04-01', 55000.00, '444-44-4444'),
    ('000005',  'CC4',  '2023-05-01', 65000.00, '555-55-5555'),
    ('000006',  'CC1',  '2023-06-01', 48000.00, '666-66-6666'),
    ('000008',  'CC9',  '2023-08-01', 52000.00, '888-88-8888'),
    ('000009',  'CC3',  '2023-09-01', 53000.00, '999-99-9999'),
    ('000010',  'CC1',  '2023-10-01', 54000.00, '101-01-0101'),
    ('000011',  'CC2',  '2099-01-01', 56000.00, '111-00-1111');
GO
INSERT INTO stg_gp.UPR00100 VALUES
    ('000001', 'D1', '2020-01-01', 50000, '111-11-1111', 0),
    ('000002', 'D2', '2021-02-01', 60000, '222-22-2222', 0),
    ('000003', 'D1', '2022-03-01', 71000, '333-33-3333', 0),
    ('000004', 'D2', '2022-04-01', 55000, '444-44-9999', 0),
    ('000005', 'D4', '2023-05-01', 65000, '555-55-5555', 0),
    ('000007', 'D1', '2023-07-01', 47000, '777-77-7777', 0),
    ('000008', 'D1', '2023-08-01', 52000, '888-88-8888', 0),
    ('000009', 'D9', '2023-09-01', 53000, '999-99-9999', 0),
    ('000010', 'D1', '2023-10-01', 54000, '101-01-0101', 0),
    ('000010', 'D1', '2023-10-01', 54000, '101-01-0101', 0),
    ('000011', 'D2', '2099-01-01', 56000, '111-00-1111', 2),
    ('ABC123', NULL, '2024-01-01', 40000, '121-21-2121', 0);
GO
-- September 2026 rows load now (fresh); the October Workday row and the August / non-UPRCC GP rows must
-- not count towards September.
INSERT INTO stg_wd.payroll_journal VALUES
    ('A6000', 1000.00, 0, '2026-09-15', SYSDATETIME()),
    ('A6100',  500.00, 0, '2026-09-20', SYSDATETIME()),
    ('A6200',  300.00, 0, '2026-09-25', SYSDATETIME()),
    ('A9999',   50.00, 0, '2026-09-26', SYSDATETIME()),
    ('A6000',  900.00, 0, '2026-10-15', SYSDATETIME());
GO
INSERT INTO stg_gp.GL20000 VALUES
    (100, 1000, 0, '2026-09-30 14:35', 'UPRCC'),
    (101,  450, 0, '2026-09-10',       'UPRCC'),
    (100,    5, 0, '2026-09-10',       'GJ'),
    (100,   20, 0, '2026-08-31 23:59', 'UPRCC');
GO
