DECLARE @period_start DATE = ?, @period_end_excl DATE = ?, @as_of DATE = ?;
DECLARE @ambiguous_wd_acct_to_gp NVARCHAR(2048);
SELECT TOP (1) @ambiguous_wd_acct_to_gp = N'ambiguous mapping wd_acct_to_gp: value ' + a.from_value
    + N' has ' + CONVERT(NVARCHAR(20), a.valid_rows) + N' valid rows'
FROM (
    SELECT UPPER(NULLIF(LTRIM(RTRIM(CONVERT(NVARCHAR(4000), mp.[wd_account], 126) COLLATE Latin1_General_100_BIN2)), N'')) AS from_value, COUNT_BIG(*) AS valid_rows
    FROM (SELECT * FROM [xref].[account_map]) AS mp
    WHERE UPPER(NULLIF(LTRIM(RTRIM(CONVERT(NVARCHAR(4000), mp.[wd_account], 126) COLLATE Latin1_General_100_BIN2)), N'')) IS NOT NULL
    GROUP BY UPPER(NULLIF(LTRIM(RTRIM(CONVERT(NVARCHAR(4000), mp.[wd_account], 126) COLLATE Latin1_General_100_BIN2)), N''))
    HAVING COUNT_BIG(*) > 1
) AS a
ORDER BY a.from_value;
IF @ambiguous_wd_acct_to_gp IS NOT NULL
    THROW 50001, @ambiguous_wd_acct_to_gp, 1;
GO
DECLARE @period_start DATE = ?, @period_end_excl DATE = ?, @as_of DATE = ?;
SELECT
    (SELECT COUNT_BIG(*) FROM (SELECT * FROM [stg_wd].[payroll_journal] WHERE [pay_date] >= @period_start AND [pay_date] < @period_end_excl) AS s) AS rows_left,
    (SELECT COUNT_BIG(*) FROM (SELECT * FROM [stg_gp].[GL20000] WHERE (SOURCDOC = 'UPRCC') AND [TRXDATE] >= @period_start AND [TRXDATE] < @period_end_excl) AS s) AS rows_right
GO
DECLARE @period_start DATE = ?, @period_end_excl DATE = ?, @as_of DATE = ?;
SELECT
    CAST(w.group_key AS NVARCHAR(400)) COLLATE DATABASE_DEFAULT AS group_key,
    CAST(w.measure AS NVARCHAR(128)) COLLATE DATABASE_DEFAULT AS measure,
    CAST(w.left_value AS DECIMAL(38,6)) AS left_value,
    CAST(w.right_value AS DECIMAL(38,6)) AS right_value,
    CAST(w.diff AS DECIMAL(38,6)) AS diff,
    CAST(w.tolerance_applied AS DECIMAL(38,6)) AS tolerance_applied,
    CAST(CASE WHEN ABS(w.diff) <= w.tolerance_applied THEN 1 ELSE 0 END AS BIT) AS within_tolerance
FROM (
    SELECT
        COALESCE(l.group_key, r.group_key) AS group_key,
        v.measure,
        v.left_value,
        v.right_value,
        v.left_value - v.right_value AS diff,
        CASE v.measure
            WHEN N'net_amount' THEN 0.01
            ELSE 0
        END AS tolerance_applied
    FROM (
        SELECT
            g.__gk AS group_key,
            -- net_amount
            CAST(COALESCE(SUM(debit - credit), 0) AS DECIMAL(38,6)) AS m1,
            -- line_count
            CAST(COALESCE(COUNT(*), 0) AS DECIMAL(38,6)) AS m2
        FROM (
            SELECT s.*, REPLACE(REPLACE(ISNULL(CASE WHEN UPPER(NULLIF(LTRIM(RTRIM(CONVERT(NVARCHAR(4000), s.[ledger_account], 126) COLLATE Latin1_General_100_BIN2)), N'')) IS NOT NULL AND gm1.[wd_account] IS NULL THEN N'UNMAPPED:' + UPPER(NULLIF(LTRIM(RTRIM(CONVERT(NVARCHAR(4000), s.[ledger_account], 126) COLLATE Latin1_General_100_BIN2)), N'')) ELSE UPPER(NULLIF(LTRIM(RTRIM(CONVERT(NVARCHAR(4000), gm1.[gp_actindx], 126) COLLATE Latin1_General_100_BIN2)), N'')) END, N'∅'), '\', N'\\'), '|', N'\|') AS __gk
            FROM (SELECT * FROM [stg_wd].[payroll_journal] WHERE [pay_date] >= @period_start AND [pay_date] < @period_end_excl) AS s
            LEFT JOIN (SELECT * FROM [xref].[account_map]) AS gm1
                ON UPPER(NULLIF(LTRIM(RTRIM(CONVERT(NVARCHAR(4000), gm1.[wd_account], 126) COLLATE Latin1_General_100_BIN2)), N'')) = UPPER(NULLIF(LTRIM(RTRIM(CONVERT(NVARCHAR(4000), s.[ledger_account], 126) COLLATE Latin1_General_100_BIN2)), N''))
        ) AS g
        GROUP BY g.__gk
    ) AS l
    FULL OUTER JOIN (
        SELECT
            g.__gk AS group_key,
            -- net_amount
            CAST(COALESCE(SUM(DEBITAMT - CRDTAMNT), 0) AS DECIMAL(38,6)) AS m1,
            -- line_count
            CAST(COALESCE(COUNT(*), 0) AS DECIMAL(38,6)) AS m2
        FROM (
            SELECT s.*, REPLACE(REPLACE(ISNULL(UPPER(NULLIF(LTRIM(RTRIM(CONVERT(NVARCHAR(4000), s.[ACTINDX], 126) COLLATE Latin1_General_100_BIN2)), N'')), N'∅'), '\', N'\\'), '|', N'\|') AS __gk
            FROM (SELECT * FROM [stg_gp].[GL20000] WHERE (SOURCDOC = 'UPRCC') AND [TRXDATE] >= @period_start AND [TRXDATE] < @period_end_excl) AS s
        ) AS g
        GROUP BY g.__gk
    ) AS r
        ON r.group_key = l.group_key
    CROSS APPLY (VALUES
        (N'net_amount', COALESCE(l.m1, 0), COALESCE(r.m1, 0)),
        (N'line_count', COALESCE(l.m2, 0), COALESCE(r.m2, 0))
    ) AS v (measure, left_value, right_value)
) AS w
GO
DECLARE @period_start DATE = ?, @period_end_excl DATE = ?, @as_of DATE = ?;
SELECT
    group_key AS business_key,
    CAST('total_mismatch' AS VARCHAR(30)) AS break_type,
    measure AS field_name,
    CONVERT(NVARCHAR(4000), left_value) AS left_value,
    CONVERT(NVARCHAR(4000), right_value) AS right_value,
    diff
FROM #totals
WHERE within_tolerance = 0