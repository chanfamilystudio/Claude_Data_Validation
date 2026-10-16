-- Sample data for the production rules (rules/) on the ASSUMED staging layout
-- (docs/ddl/assumed_staging.sql). Period under test: 2026-09. Every planted break is
-- listed in tests/integration/test_production_rules.py.

------------------------------------------------------------------ crosswalks
INSERT INTO xref.cost_center_map (wd_cost_center, gp_dept, eff_start, eff_end) VALUES
 (N'CC100', N'D100', '2000-01-01', NULL),
 (N'CC200', N'D150', '2000-01-01', '2025-12-31'),       -- re-mapped from 2026
 (N'CC200', N'D200', '2026-01-01', NULL),
 (N'CC300', N'D300', '2000-01-01', NULL);               -- D300 missing in GP -> mapping_mismatch
INSERT INTO xref.job_profile_map VALUES (N'JP-ACC', N'ACCT'), (N'JP-ENG', N'ENG');
INSERT INTO xref.worker_status_map VALUES (N'Active', 0), (N'Leave', 0), (N'Terminated', 1);
INSERT INTO xref.account_map VALUES (N'6000', N'6000'), (N'6100', N'6100'), (N'2100', N'2100'),
                                    (N'7000', N'7000');  -- 7000 not a GP natural account
INSERT INTO xref.supplier_status_map VALUES (N'Active', 1), (N'Inactive', 2), (N'On Hold', 3);
GO

------------------------------------------------------------------ employees
INSERT INTO stg_wd.workers VALUES
 (N'1001', N'Ana',   N'Lopez', N'CC100', N'JP-ACC', '2020-01-15', N'Active',     N'111-11-1111', SYSDATETIME()),
 (N'1002', N'Ben',   N'Smith', N'CC100', N'JP-ACC', '2021-03-01', N'Active',     N'222-22-2222', SYSDATETIME()),
 (N'1003', N'Cai',   N'Wong',  N'CC200', N'JP-ENG', '2022-06-01', N'Active',     N'333-33-3333', SYSDATETIME()),
 (N'1004', N'Dee',   N'Ray',   N'CC999', N'JP-ACC', '2023-02-01', N'Active',     N'444-44-4444', SYSDATETIME()),
 (N'1005', N'Eli',   N'Fox',   N'CC100', N'JP-ACC', '2024-04-01', N'Active',     N'555-55-5555', SYSDATETIME()),
 (N'1007', N'Gus',   N'Hall',  N'CC100', N'JP-ACC', '2019-09-09', N'Active',     N'777-77-7777', SYSDATETIME()),
 (N'1008', N'Hal',   N'Ives',  N'CC100', N'JP-ACC', '2018-08-08', N'Terminated', N'888-88-8888', SYSDATETIME());
INSERT INTO stg_wd.cost_centers VALUES
 (N'CC100', N'Finance', 1, SYSDATETIME()), (N'CC200', N'Engineering', 1, SYSDATETIME()),
 (N'CC300', N'Legal', 1, SYSDATETIME()),   (N'CC999', N'Closed unit', 0, SYSDATETIME());
INSERT INTO stg_gp.UPR40300 (DEPRTMNT, DSCRIPTN) VALUES ('D100', 'Finance'), ('D150', 'Eng (old)'), ('D200', 'Engineering');
INSERT INTO stg_gp.UPR40301 (JOBTITLE, DSCRIPTN) VALUES ('ACCT', 'Accountant'), ('ENG', 'Engineer');
INSERT INTO stg_gp.UPR00100 (EMPLOYID, LASTNAME, FRSTNAME, DEPRTMNT, JOBTITLE, STRTDATE, INACTIVE, SOCSCNUM) VALUES
 ('1001', 'LOPEZ', 'ANA', 'D100', 'ACCT', '2020-01-15', 0, '111-11-1111'),     -- clean (case-insensitive)
 ('1002', 'Smyth', 'Ben', 'D100', 'ACCT', '2021-03-01', 0, '222-22-2222'),     -- last name differs
 ('1003', 'Wong',  'Cai', 'D100', 'ENG',  '2022-06-01', 0, '333-33-3333'),     -- dept differs (CC200 -> D200)
 ('1004', 'Ray',   'Dee', 'D100', 'ACCT', '2023-02-01', 0, '444-44-4444'),     -- WD cost center unmapped
 ('1006', 'Fay',   'Gil', 'D999', 'ACCT', '2020-05-05', 0, '666-66-6666'),     -- GP only; D999 unknown dept
 ('1007', 'Hall',  'Gus', 'D100', 'ACCT', '2019-09-09', 0, '777-77-7770'),     -- national id differs (PII)
 ('1008', 'Ives',  'Hal', 'D100', 'ACCT', '2018-08-08', 0, '888-88-8888');     -- status differs
GO

------------------------------------------------------------------ chart of accounts
INSERT INTO stg_wd.ledger_accounts VALUES
 (N'6000', N'Salaries', 1, SYSDATETIME()), (N'6100', N'Benefits', 1, SYSDATETIME()),
 (N'2100', N'Payroll clearing', 1, SYSDATETIME()), (N'7000', N'Travel', 1, SYSDATETIME()),
 (N'8000', N'Training', 1, SYSDATETIME()),          -- active, no crosswalk row -> mapping_missing
 (N'9000', N'Retired', 0, SYSDATETIME());           -- inactive: ignored
INSERT INTO stg_gp.GL00100 (ACTINDX, ACTNUMBR_1, ACTNUMBR_2, ACTNUMBR_3, ACTDESCR, ACTIVE) VALUES
 (1, '100', '6000', 'D100', 'Salaries Finance', 1),
 (2, '100', '6100', 'D200', 'Benefits Engineering', 1),
 (3, '100', '2100', 'D100', 'Payroll clearing', 1),
 (4, '100', '6000', 'D200', 'Salaries Engineering', 1);
INSERT INTO stg_gp.GL00105 (ACTINDX, ACTNUMST) VALUES
 (1, '100-6000-D100'), (2, '100-6100-D200'), (3, '100-2100-D100'), (4, '100-6000-D200'),
 (5, '100-9999-D100');                                 -- no GL00100 row -> references violation
GO

------------------------------------------------------------------ payroll (September 2026)
INSERT INTO stg_wd.payroll_journal (journal_id, line_id, pay_date, ledger_account, cost_center, debit, credit, load_ts) VALUES
 (N'J1', 1, '2026-09-15', N'6000', N'CC100', 1000.00,    0, SYSDATETIME()),
 (N'J1', 2, '2026-09-15', N'6100', N'CC200',  500.00,    0, SYSDATETIME()),
 (N'J1', 3, '2026-09-15', N'2100', N'CC100',     0, 1500.00, SYSDATETIME()),
 (N'J2', 1, '2026-09-30', N'6000', N'CC100',  200.00,    0, SYSDATETIME()),
 (N'J2', 2, '2026-09-30', N'2100', N'CC100',     0,  199.95, SYSDATETIME()),  -- J2 unbalanced by 0.05
 (N'J9', 1, '2026-10-01', N'6000', N'CC100', 9999.00,    0, SYSDATETIME());   -- October: ignored
INSERT INTO stg_gp.GL20000 (JRNENTRY, SOURCDOC, TRXDATE, ACTINDX, DEBITAMT, CRDTAMNT) VALUES
 (501, 'UPRCC', '2026-09-15 10:00', 1, 1000, 0),
 (501, 'UPRCC', '2026-09-15 10:00', 2,  500, 0),
 (501, 'UPRCC', '2026-09-15 10:00', 3,    0, 1500),
 (502, 'UPRCC', '2026-09-30 17:45', 1,  200, 0),       -- late on the last day: still September
 (502, 'UPRCC', '2026-09-30 17:45', 3,    0, 200),
 (503, 'GJ',    '2026-09-20',       1,  777, 0),       -- not payroll: filtered out
 (504, 'UPRCC', '2026-10-01',       1,  888, 0);       -- October: ignored
GO

------------------------------------------------------------------ AP
INSERT INTO stg_wd.suppliers VALUES
 (N'V001', N'Acme Corp',   N'Active',   N'12-3456789', N'Austin',  N'TX', N'78701', N'USD', SYSDATETIME()),
 (N'V002', N'Bolt Ltd',    N'Active',   N'98-7654321', N'Dallas',  N'TX', N'75201', N'USD', SYSDATETIME()),
 (N'V003', N'Crane Inc',   N'Inactive', N'55-5555555', N'Houston', N'TX', N'77001', N'USD', SYSDATETIME());
INSERT INTO stg_gp.PM00200 (VENDORID, VENDNAME, VENDSTTS, TXIDNMBR, CITY, STATE, ZIPCODE, CURNCYID) VALUES
 ('V001', 'ACME CORP', 1, '12-3456789', 'Austin',     'TX', '78701', 'USD'),  -- clean
 ('V002', 'Bolt Ltd',  1, '98-7654321', 'Fort Worth', 'TX', '75201', 'USD'),  -- city differs
 ('V003', 'Crane Inc', 1, '55-5555555', 'Houston',    'TX', '77001', 'USD'),  -- status differs
 ('V004', 'Delta LLC', 1, '44-4444444', 'Austin',     'TX', '78702', 'USD');  -- GP only
INSERT INTO stg_wd.supplier_invoices VALUES
 (N'V001', N'INV-1',  '2026-09-05', 1000.00, N'USD', SYSDATETIME()),
 (N'V001', N'INV-2',  '2026-09-12',  250.00, N'USD', SYSDATETIME()),
 (N'V002', N'INV-9',  '2026-09-20',   75.50, N'USD', SYSDATETIME()),
 (N'V005', N'INV-77', '2026-09-25',   10.00, N'USD', SYSDATETIME());   -- supplier unknown in Workday master; not in GP
INSERT INTO stg_gp.PM30200 (VCHRNMBR, VENDORID, DOCTYPE, DOCNUMBR, DOCDATE, DOCAMNT, VOIDED) VALUES
 ('00001', 'V001', 1, 'INV-1', '2026-09-05', 1000.00000, 0);
INSERT INTO stg_gp.PM20000 (VCHRNMBR, VENDORID, DOCTYPE, DOCNUMBR, DOCDATE, DOCAMNT, VOIDED) VALUES
 ('00002', 'V001', 1, 'INV-2',  '2026-09-12', 250.00400, 0),   -- within 0.01
 ('00003', 'V002', 1, 'INV-9',  '2026-09-20',  75.00000, 0),   -- amount differs
 ('00004', 'V002', 1, 'INV-10', '2026-09-22',  30.00000, 0),   -- GP only
 ('00005', 'V002', 1, 'INV-11', '2026-09-23',  99.00000, 1),   -- voided: ignored
 ('00006', 'V002', 5, 'PMT-1',  '2026-09-24', 105.00000, 0);   -- payment, not an invoice: ignored
GO
