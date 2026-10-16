DECLARE @period_start DATE = ?, @period_end_excl DATE = ?, @as_of DATE = ?;
DECLARE @ambiguous_cc_to_dept NVARCHAR(2048);
SELECT TOP (1) @ambiguous_cc_to_dept = N'ambiguous mapping cc_to_dept: value ' + a.from_value
    + N' has ' + CONVERT(NVARCHAR(20), a.valid_rows) + N' valid rows'
FROM (
    SELECT UPPER(NULLIF(LTRIM(RTRIM(CONVERT(NVARCHAR(4000), mp.[wd_cost_center], 126) COLLATE Latin1_General_100_BIN2)), N'')) AS from_value, COUNT_BIG(*) AS valid_rows
    FROM (SELECT * FROM [xref].[cost_center_map] WHERE [eff_start] <= @as_of AND ([eff_end] IS NULL OR [eff_end] >= @as_of)) AS mp
    WHERE UPPER(NULLIF(LTRIM(RTRIM(CONVERT(NVARCHAR(4000), mp.[wd_cost_center], 126) COLLATE Latin1_General_100_BIN2)), N'')) IS NOT NULL
    GROUP BY UPPER(NULLIF(LTRIM(RTRIM(CONVERT(NVARCHAR(4000), mp.[wd_cost_center], 126) COLLATE Latin1_General_100_BIN2)), N''))
    HAVING COUNT_BIG(*) > 1
) AS a
ORDER BY a.from_value;
IF @ambiguous_cc_to_dept IS NOT NULL
    THROW 50001, @ambiguous_cc_to_dept, 1;
GO
DECLARE @period_start DATE = ?, @period_end_excl DATE = ?, @as_of DATE = ?;
SELECT
    (SELECT COUNT_BIG(*) FROM (SELECT * FROM [stg_wd].[workers]) AS s) AS rows_left,
    (SELECT COUNT_BIG(*) FROM (SELECT * FROM [stg_gp].[UPR00100]) AS s) AS rows_right
GO
DECLARE @period_start DATE = ?, @period_end_excl DATE = ?, @as_of DATE = ?;
SELECT
    CAST(k.business_key AS NVARCHAR(400)) COLLATE DATABASE_DEFAULT AS business_key,
    CAST(b.break_type AS VARCHAR(30)) AS break_type,
    CAST(b.field_name AS NVARCHAR(128)) AS field_name,
    CAST(b.left_value AS NVARCHAR(4000)) COLLATE DATABASE_DEFAULT AS left_value,
    CAST(b.right_value AS NVARCHAR(4000)) COLLATE DATABASE_DEFAULT AS right_value,
    CAST(b.diff AS DECIMAL(38,6)) AS diff
FROM (
    SELECT
        COALESCE(l.business_key, r.business_key) AS business_key,
        l.key_rows AS l_rows,
        r.key_rows AS r_rows,
        -- f1: cost_center via cc_to_dept vs DEPRTMNT (string)
        CASE
            WHEN l.f1_unmapped = 1 THEN 'mapping_missing'
            WHEN l.f1_val = r.f1_val OR (l.f1_val IS NULL AND r.f1_val IS NULL) THEN NULL
            ELSE 'value_mismatch'
        END AS f1_break,
        CASE WHEN l.f1_unmapped = 1 THEN l.f1_src ELSE l.f1_val END AS f1_left,
        r.f1_val AS f1_right,
        CAST(NULL AS DECIMAL(38,6)) AS f1_diff,
        -- f2: hire_date vs STRTDATE (date)
        CASE
            WHEN (l.f2_val IS NULL AND NULLIF(LTRIM(RTRIM(l.f2_raw)), N'') IS NOT NULL) OR (r.f2_val IS NULL AND NULLIF(LTRIM(RTRIM(r.f2_raw)), N'') IS NOT NULL) THEN 'value_mismatch'
            WHEN l.f2_val = r.f2_val OR (l.f2_val IS NULL AND r.f2_val IS NULL) THEN NULL
            ELSE 'value_mismatch'
        END AS f2_break,
        CASE WHEN (l.f2_val IS NULL AND NULLIF(LTRIM(RTRIM(l.f2_raw)), N'') IS NOT NULL) THEN l.f2_raw ELSE CONVERT(NVARCHAR(4000), l.f2_val) END AS f2_left,
        CASE WHEN (r.f2_val IS NULL AND NULLIF(LTRIM(RTRIM(r.f2_raw)), N'') IS NOT NULL) THEN r.f2_raw ELSE CONVERT(NVARCHAR(4000), r.f2_val) END AS f2_right,
        CAST(NULL AS DECIMAL(38,6)) AS f2_diff,
        -- f3: annual_salary vs SALARY (decimal)
        CASE
            WHEN (l.f3_val IS NULL AND NULLIF(LTRIM(RTRIM(l.f3_raw)), N'') IS NOT NULL) OR (r.f3_val IS NULL AND NULLIF(LTRIM(RTRIM(r.f3_raw)), N'') IS NOT NULL) THEN 'value_mismatch'
            WHEN (l.f3_val IS NULL AND r.f3_val IS NULL) OR ABS(l.f3_val - r.f3_val) <= 0.01 THEN NULL
            ELSE 'value_mismatch'
        END AS f3_break,
        CASE WHEN (l.f3_val IS NULL AND NULLIF(LTRIM(RTRIM(l.f3_raw)), N'') IS NOT NULL) THEN l.f3_raw ELSE CONVERT(NVARCHAR(4000), l.f3_val) END AS f3_left,
        CASE WHEN (r.f3_val IS NULL AND NULLIF(LTRIM(RTRIM(r.f3_raw)), N'') IS NOT NULL) THEN r.f3_raw ELSE CONVERT(NVARCHAR(4000), r.f3_val) END AS f3_right,
        l.f3_val - r.f3_val AS f3_diff,
        -- f4: national_id vs SOCSCNUM (string, pii)
        CASE
            WHEN l.f4_val = r.f4_val OR (l.f4_val IS NULL AND r.f4_val IS NULL) THEN NULL
            ELSE 'value_mismatch'
        END AS f4_break,
        N'***' AS f4_left,
        N'***' AS f4_right,
        CAST(NULL AS DECIMAL(38,6)) AS f4_diff
    FROM (
        SELECT
            base.business_key,
            COUNT_BIG(*) AS key_rows,
            MAX(base.f1_src) AS f1_src,
            MAX(base.f1_unmapped) AS f1_unmapped,
            MAX(base.f1_val) AS f1_val,
            MAX(base.f2_val) AS f2_val,
            MAX(base.f2_raw) AS f2_raw,
            MAX(base.f3_val) AS f3_val,
            MAX(base.f3_raw) AS f3_raw,
            MAX(base.f4_val) AS f4_val
        FROM (
            SELECT
                REPLACE(REPLACE(ISNULL(UPPER(NULLIF(LTRIM(RTRIM(CONVERT(NVARCHAR(4000), s.[employee_id], 126) COLLATE Latin1_General_100_BIN2)), N'')), N'∅'), '\', N'\\'), '|', N'\|') AS business_key,
                -- f1: cost_center via cc_to_dept vs DEPRTMNT (string)
                CONVERT(NVARCHAR(4000), s.[cost_center], 126) AS f1_src,
                CASE WHEN UPPER(NULLIF(LTRIM(RTRIM(CONVERT(NVARCHAR(4000), s.[cost_center], 126) COLLATE Latin1_General_100_BIN2)), N'')) IS NOT NULL AND m1.[wd_cost_center] IS NULL THEN 1 ELSE 0 END AS f1_unmapped,
                UPPER(NULLIF(LTRIM(RTRIM(CONVERT(NVARCHAR(4000), m1.[gp_dept], 126) COLLATE Latin1_General_100_BIN2)), N'')) AS f1_val,
                -- f2: hire_date vs STRTDATE (date)
                TRY_CAST(CASE WHEN LTRIM(RTRIM(CONVERT(NVARCHAR(4000), s.[hire_date], 126))) = N'' THEN NULL ELSE s.[hire_date] END AS DATE) AS f2_val,
                CONVERT(NVARCHAR(4000), s.[hire_date], 126) AS f2_raw,
                -- f3: annual_salary vs SALARY (decimal)
                TRY_CAST(CASE WHEN LTRIM(RTRIM(CONVERT(NVARCHAR(4000), s.[annual_salary], 126))) = N'' THEN NULL ELSE s.[annual_salary] END AS DECIMAL(38,6)) AS f3_val,
                CONVERT(NVARCHAR(4000), s.[annual_salary], 126) AS f3_raw,
                -- f4: national_id vs SOCSCNUM (string, pii)
                HASHBYTES('SHA2_256', UPPER(NULLIF(LTRIM(RTRIM(CONVERT(NVARCHAR(4000), s.[national_id], 126) COLLATE Latin1_General_100_BIN2)), N''))) AS f4_val
            FROM (SELECT * FROM [stg_wd].[workers]) AS s
            LEFT JOIN (SELECT * FROM [xref].[cost_center_map] WHERE [eff_start] <= @as_of AND ([eff_end] IS NULL OR [eff_end] >= @as_of)) AS m1
                ON UPPER(NULLIF(LTRIM(RTRIM(CONVERT(NVARCHAR(4000), m1.[wd_cost_center], 126) COLLATE Latin1_General_100_BIN2)), N'')) = UPPER(NULLIF(LTRIM(RTRIM(CONVERT(NVARCHAR(4000), s.[cost_center], 126) COLLATE Latin1_General_100_BIN2)), N''))
        ) AS base
        GROUP BY base.business_key
    ) AS l
    FULL OUTER JOIN (
        SELECT
            base.business_key,
            COUNT_BIG(*) AS key_rows,
            MAX(base.f1_val) AS f1_val,
            MAX(base.f2_val) AS f2_val,
            MAX(base.f2_raw) AS f2_raw,
            MAX(base.f3_val) AS f3_val,
            MAX(base.f3_raw) AS f3_raw,
            MAX(base.f4_val) AS f4_val
        FROM (
            SELECT
                REPLACE(REPLACE(ISNULL(UPPER(NULLIF(LTRIM(RTRIM(CONVERT(NVARCHAR(4000), s.[EMPLOYID], 126) COLLATE Latin1_General_100_BIN2)), N'')), N'∅'), '\', N'\\'), '|', N'\|') AS business_key,
                -- f1: cost_center via cc_to_dept vs DEPRTMNT (string)
                UPPER(NULLIF(LTRIM(RTRIM(CONVERT(NVARCHAR(4000), s.[DEPRTMNT], 126) COLLATE Latin1_General_100_BIN2)), N'')) AS f1_val,
                -- f2: hire_date vs STRTDATE (date)
                TRY_CAST(CASE WHEN LTRIM(RTRIM(CONVERT(NVARCHAR(4000), s.[STRTDATE], 126))) = N'' THEN NULL ELSE s.[STRTDATE] END AS DATE) AS f2_val,
                CONVERT(NVARCHAR(4000), s.[STRTDATE], 126) AS f2_raw,
                -- f3: annual_salary vs SALARY (decimal)
                TRY_CAST(CASE WHEN LTRIM(RTRIM(CONVERT(NVARCHAR(4000), s.[SALARY], 126))) = N'' THEN NULL ELSE s.[SALARY] END AS DECIMAL(38,6)) AS f3_val,
                CONVERT(NVARCHAR(4000), s.[SALARY], 126) AS f3_raw,
                -- f4: national_id vs SOCSCNUM (string, pii)
                HASHBYTES('SHA2_256', UPPER(NULLIF(LTRIM(RTRIM(CONVERT(NVARCHAR(4000), s.[SOCSCNUM], 126) COLLATE Latin1_General_100_BIN2)), N''))) AS f4_val
            FROM (SELECT * FROM [stg_gp].[UPR00100]) AS s
        ) AS base
        GROUP BY base.business_key
    ) AS r
        ON r.business_key = l.business_key
) AS k
CROSS APPLY (
    -- duplicate keys: one quality_violation each (row counts), excluded from all other checks
    SELECT 'quality_violation', N'__key__', CONVERT(NVARCHAR(4000), ISNULL(k.l_rows, 0)), CONVERT(NVARCHAR(4000), ISNULL(k.r_rows, 0)), CAST(NULL AS DECIMAL(38,6))
    WHERE k.l_rows > 1 OR k.r_rows > 1
    UNION ALL
    SELECT 'missing_left', NULL, NULL, NULL, NULL
    WHERE k.l_rows IS NULL AND k.r_rows = 1
    UNION ALL
    SELECT 'missing_right', NULL, NULL, NULL, NULL
    WHERE k.l_rows = 1 AND k.r_rows IS NULL
    UNION ALL
    -- matched keys: one row per breaking compare field
    SELECT f.break_type, f.field_name, f.left_value, f.right_value, f.diff
    FROM (VALUES
        (k.f1_break, N'cost_center', k.f1_left, k.f1_right, k.f1_diff),
        (k.f2_break, N'hire_date', k.f2_left, k.f2_right, k.f2_diff),
        (k.f3_break, N'annual_salary', k.f3_left, k.f3_right, k.f3_diff),
        (k.f4_break, N'national_id', k.f4_left, k.f4_right, k.f4_diff)
    ) AS f (break_type, field_name, left_value, right_value, diff)
    WHERE k.l_rows = 1 AND k.r_rows = 1 AND f.break_type IS NOT NULL
) AS b (break_type, field_name, left_value, right_value, diff)