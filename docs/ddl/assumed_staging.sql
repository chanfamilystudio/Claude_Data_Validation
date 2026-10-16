-- ASSUMED staging layout for the production rules in rules/.
--
-- Nothing here is your real DDL yet. It documents what config/datasets.yaml and
-- config/mappings.yaml expect, so the rules can be built, validated and tested now.
-- When the real staging DDL is available, either make staging match this file or
-- change the object/column names in config/datasets.yaml, config/mappings.yaml and
-- the rule files (docs/production-rules.md lists every assumption).
--
--   stg_wd.*  Workday extracts (RaaS/EIB/API), one row per business object, with load_ts
--   stg_gp.*  straight copies of Dynamics GP tables (GP names, types and fixed-width CHAR)
--             plus three helper views over them
--   xref.*    crosswalk tables maintained by finance/HRIS
--
-- Run with sqlcmd or `recon`'s test harness; batches are separated by GO.

IF SCHEMA_ID('stg_wd') IS NULL EXEC('CREATE SCHEMA stg_wd');
GO
IF SCHEMA_ID('stg_gp') IS NULL EXEC('CREATE SCHEMA stg_gp');
GO
IF SCHEMA_ID('xref') IS NULL EXEC('CREATE SCHEMA xref');
GO

-------------------------------------------------------------------------------
-- Workday staging
-------------------------------------------------------------------------------
DROP TABLE IF EXISTS stg_wd.workers;
CREATE TABLE stg_wd.workers (
    employee_id       NVARCHAR(15)  NOT NULL,   -- = GP EMPLOYID
    legal_first_name  NVARCHAR(50)  NULL,
    legal_last_name   NVARCHAR(50)  NULL,
    cost_center       NVARCHAR(20)  NULL,       -- Workday cost center reference ID
    job_profile       NVARCHAR(50)  NULL,       -- Workday job profile reference ID
    hire_date         DATE          NULL,
    worker_status     NVARCHAR(20)  NULL,       -- Active | Terminated | Leave
    national_id       NVARCHAR(20)  NULL,       -- PII
    load_ts           DATETIME2(3)  NOT NULL
);
GO
DROP TABLE IF EXISTS stg_wd.cost_centers;
CREATE TABLE stg_wd.cost_centers (
    cost_center_id    NVARCHAR(20)  NOT NULL,
    cost_center_name  NVARCHAR(100) NULL,
    is_active         BIT           NOT NULL,
    load_ts           DATETIME2(3)  NOT NULL
);
GO
DROP TABLE IF EXISTS stg_wd.ledger_accounts;
CREATE TABLE stg_wd.ledger_accounts (
    ledger_account    NVARCHAR(20)  NOT NULL,
    account_name      NVARCHAR(100) NULL,
    is_active         BIT           NOT NULL,
    load_ts           DATETIME2(3)  NOT NULL
);
GO
DROP TABLE IF EXISTS stg_wd.payroll_journal;
CREATE TABLE stg_wd.payroll_journal (
    journal_id        NVARCHAR(30)  NOT NULL,
    line_id           INT           NOT NULL,
    pay_date          DATE          NOT NULL,   -- period column
    ledger_account    NVARCHAR(20)  NOT NULL,
    cost_center       NVARCHAR(20)  NULL,
    debit             DECIMAL(19,2) NOT NULL DEFAULT 0,
    credit            DECIMAL(19,2) NOT NULL DEFAULT 0,
    load_ts           DATETIME2(3)  NOT NULL
);
GO
DROP TABLE IF EXISTS stg_wd.suppliers;
CREATE TABLE stg_wd.suppliers (
    supplier_id       NVARCHAR(15)  NOT NULL,   -- = GP VENDORID
    supplier_name     NVARCHAR(65)  NULL,
    supplier_status   NVARCHAR(20)  NULL,       -- Active | Inactive | On Hold
    tax_id            NVARCHAR(20)  NULL,       -- PII
    city              NVARCHAR(35)  NULL,
    state             NVARCHAR(29)  NULL,
    postal_code       NVARCHAR(11)  NULL,
    currency          NVARCHAR(15)  NULL,
    load_ts           DATETIME2(3)  NOT NULL
);
GO
DROP TABLE IF EXISTS stg_wd.supplier_invoices;
CREATE TABLE stg_wd.supplier_invoices (
    supplier_id       NVARCHAR(15)  NOT NULL,
    invoice_number    NVARCHAR(21)  NOT NULL,   -- = GP DOCNUMBR
    invoice_date      DATE          NOT NULL,   -- period column
    invoice_amount    DECIMAL(19,2) NOT NULL,
    currency          NVARCHAR(15)  NULL,
    load_ts           DATETIME2(3)  NOT NULL
);
GO

-------------------------------------------------------------------------------
-- GP straight copies (subset of columns the rules use; extra columns are fine)
-------------------------------------------------------------------------------
DROP TABLE IF EXISTS stg_gp.UPR00100;                -- employee master
CREATE TABLE stg_gp.UPR00100 (
    EMPLOYID  CHAR(15)  NOT NULL,
    LASTNAME  CHAR(21)  NOT NULL DEFAULT '',
    FRSTNAME  CHAR(15)  NOT NULL DEFAULT '',
    DEPRTMNT  CHAR(7)   NOT NULL DEFAULT '',
    JOBTITLE  CHAR(7)   NOT NULL DEFAULT '',
    STRTDATE  DATETIME  NOT NULL DEFAULT '1900-01-01',
    INACTIVE  TINYINT   NOT NULL DEFAULT 0,
    SOCSCNUM  CHAR(15)  NOT NULL DEFAULT ''
);
GO
DROP TABLE IF EXISTS stg_gp.UPR40300;                -- departments
CREATE TABLE stg_gp.UPR40300 (
    DEPRTMNT  CHAR(7)   NOT NULL,
    DSCRIPTN  CHAR(31)  NOT NULL DEFAULT ''
);
GO
DROP TABLE IF EXISTS stg_gp.UPR40301;                -- job titles
CREATE TABLE stg_gp.UPR40301 (
    JOBTITLE  CHAR(7)   NOT NULL,
    DSCRIPTN  CHAR(31)  NOT NULL DEFAULT ''
);
GO
DROP TABLE IF EXISTS stg_gp.GL00100;                 -- account master (segments)
CREATE TABLE stg_gp.GL00100 (
    ACTINDX   INT       NOT NULL,
    ACTNUMBR_1 CHAR(9)  NOT NULL DEFAULT '',         -- company
    ACTNUMBR_2 CHAR(9)  NOT NULL DEFAULT '',         -- natural account
    ACTNUMBR_3 CHAR(9)  NOT NULL DEFAULT '',         -- department
    ACTDESCR  CHAR(51)  NOT NULL DEFAULT '',
    ACTIVE    TINYINT   NOT NULL DEFAULT 1
);
GO
DROP TABLE IF EXISTS stg_gp.GL00105;                 -- account index -> full account string
CREATE TABLE stg_gp.GL00105 (
    ACTINDX   INT       NOT NULL,
    ACTNUMST  CHAR(129) NOT NULL
);
GO
DROP TABLE IF EXISTS stg_gp.GL20000;                 -- open-year GL transactions
CREATE TABLE stg_gp.GL20000 (
    JRNENTRY  INT            NOT NULL,
    SOURCDOC  CHAR(11)       NOT NULL DEFAULT '',
    TRXDATE   DATETIME       NOT NULL,
    ACTINDX   INT            NOT NULL,
    DEBITAMT  NUMERIC(19,5)  NOT NULL DEFAULT 0,
    CRDTAMNT  NUMERIC(19,5)  NOT NULL DEFAULT 0,
    REFRENCE  CHAR(31)       NOT NULL DEFAULT ''
);
GO
DROP TABLE IF EXISTS stg_gp.PM00200;                 -- vendor master
CREATE TABLE stg_gp.PM00200 (
    VENDORID  CHAR(15)  NOT NULL,
    VENDNAME  CHAR(65)  NOT NULL DEFAULT '',
    VENDSTTS  SMALLINT  NOT NULL DEFAULT 1,          -- 1 active, 2 inactive, 3 temporary
    TXIDNMBR  CHAR(11)  NOT NULL DEFAULT '',
    CITY      CHAR(35)  NOT NULL DEFAULT '',
    STATE     CHAR(29)  NOT NULL DEFAULT '',
    ZIPCODE   CHAR(11)  NOT NULL DEFAULT '',
    CURNCYID  CHAR(15)  NOT NULL DEFAULT ''
);
GO
DROP TABLE IF EXISTS stg_gp.PM20000;                 -- open payables documents
CREATE TABLE stg_gp.PM20000 (
    VCHRNMBR  CHAR(21)       NOT NULL,
    VENDORID  CHAR(15)       NOT NULL,
    DOCTYPE   SMALLINT       NOT NULL,               -- 1 = invoice
    DOCNUMBR  CHAR(21)       NOT NULL,
    DOCDATE   DATETIME       NOT NULL,
    DOCAMNT   NUMERIC(19,5)  NOT NULL,
    VOIDED    TINYINT        NOT NULL DEFAULT 0
);
GO
DROP TABLE IF EXISTS stg_gp.PM30200;                 -- historical (paid) payables documents
CREATE TABLE stg_gp.PM30200 (
    VCHRNMBR  CHAR(21)       NOT NULL,
    VENDORID  CHAR(15)       NOT NULL,
    DOCTYPE   SMALLINT       NOT NULL,
    DOCNUMBR  CHAR(21)       NOT NULL,
    DOCDATE   DATETIME       NOT NULL,
    DOCAMNT   NUMERIC(19,5)  NOT NULL,
    VOIDED    TINYINT        NOT NULL DEFAULT 0
);
GO

-------------------------------------------------------------------------------
-- GP helper views (the only non-GP objects on the GP side)
-------------------------------------------------------------------------------
CREATE OR ALTER VIEW stg_gp.gl_detail AS          -- GL lines with account segments
SELECT g.JRNENTRY, g.SOURCDOC, g.TRXDATE, g.ACTINDX,
       a.ACTNUMBR_2 AS natural_account,
       a.ACTNUMBR_3 AS department_segment,
       g.DEBITAMT, g.CRDTAMNT
FROM stg_gp.GL20000 g
JOIN stg_gp.GL00100 a ON a.ACTINDX = g.ACTINDX;
GO
CREATE OR ALTER VIEW stg_gp.natural_accounts AS   -- one row per active natural account
SELECT DISTINCT ACTNUMBR_2 AS natural_account
FROM stg_gp.GL00100
WHERE ACTIVE = 1;
GO
CREATE OR ALTER VIEW stg_gp.ap_invoices AS        -- open + paid, non-voided invoices
SELECT VENDORID, DOCNUMBR, DOCDATE, DOCAMNT, VCHRNMBR FROM stg_gp.PM20000 WHERE DOCTYPE = 1 AND VOIDED = 0
UNION ALL
SELECT VENDORID, DOCNUMBR, DOCDATE, DOCAMNT, VCHRNMBR FROM stg_gp.PM30200 WHERE DOCTYPE = 1 AND VOIDED = 0;
GO

-------------------------------------------------------------------------------
-- Crosswalks
-------------------------------------------------------------------------------
DROP TABLE IF EXISTS xref.cost_center_map;        -- Workday cost center -> GP department
CREATE TABLE xref.cost_center_map (
    wd_cost_center NVARCHAR(20) NOT NULL,
    gp_dept        NVARCHAR(7)  NOT NULL,
    eff_start      DATE         NOT NULL,
    eff_end        DATE         NULL
);
GO
DROP TABLE IF EXISTS xref.job_profile_map;        -- Workday job profile -> GP job title
CREATE TABLE xref.job_profile_map (
    wd_job_profile NVARCHAR(50) NOT NULL,
    gp_jobtitle    NVARCHAR(7)  NOT NULL
);
GO
DROP TABLE IF EXISTS xref.worker_status_map;      -- Workday worker status -> GP INACTIVE flag
CREATE TABLE xref.worker_status_map (
    wd_status      NVARCHAR(20) NOT NULL,
    gp_inactive    TINYINT      NOT NULL
);
GO
DROP TABLE IF EXISTS xref.account_map;            -- Workday ledger account -> GP natural account
CREATE TABLE xref.account_map (
    wd_ledger_account NVARCHAR(20) NOT NULL,
    gp_natural_account NVARCHAR(9) NOT NULL
);
GO
DROP TABLE IF EXISTS xref.supplier_status_map;    -- Workday supplier status -> GP VENDSTTS
CREATE TABLE xref.supplier_status_map (
    wd_status      NVARCHAR(20) NOT NULL,
    gp_vendstts    SMALLINT     NOT NULL
);
GO
