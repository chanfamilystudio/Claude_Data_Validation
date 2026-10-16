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
        -- f1: annual_salary vs SALARY (int)
        CASE
            WHEN (l.f1_val IS NULL AND NULLIF(LTRIM(RTRIM(l.f1_raw)), N'') IS NOT NULL) OR (r.f1_val IS NULL AND NULLIF(LTRIM(RTRIM(r.f1_raw)), N'') IS NOT NULL) THEN 'value_mismatch'
            WHEN (l.f1_val IS NULL AND r.f1_val IS NULL) OR ABS(CONVERT(DECIMAL(19,0), l.f1_val) - CONVERT(DECIMAL(19,0), r.f1_val)) <= CASE WHEN 2 >= 0.05 * ABS(r.f1_val) THEN 2 ELSE 0.05 * ABS(r.f1_val) END THEN NULL
            ELSE 'value_mismatch'
        END AS f1_break,
        CASE WHEN (l.f1_val IS NULL AND NULLIF(LTRIM(RTRIM(l.f1_raw)), N'') IS NOT NULL) THEN l.f1_raw ELSE CONVERT(NVARCHAR(4000), l.f1_val) END AS f1_left,
        CASE WHEN (r.f1_val IS NULL AND NULLIF(LTRIM(RTRIM(r.f1_raw)), N'') IS NOT NULL) THEN r.f1_raw ELSE CONVERT(NVARCHAR(4000), r.f1_val) END AS f1_right,
        CONVERT(DECIMAL(19,0), l.f1_val) - CONVERT(DECIMAL(19,0), r.f1_val) AS f1_diff,
        -- f2: active vs INACTIVE (bool)
        CASE
            WHEN (l.f2_val IS NULL AND NULLIF(LTRIM(RTRIM(l.f2_raw)), N'') IS NOT NULL) OR (r.f2_val IS NULL AND NULLIF(LTRIM(RTRIM(r.f2_raw)), N'') IS NOT NULL) THEN 'value_mismatch'
            WHEN l.f2_val = r.f2_val OR (l.f2_val IS NULL AND r.f2_val IS NULL) THEN NULL
            ELSE 'value_mismatch'
        END AS f2_break,
        CASE WHEN (l.f2_val IS NULL AND NULLIF(LTRIM(RTRIM(l.f2_raw)), N'') IS NOT NULL) THEN l.f2_raw ELSE CONVERT(NVARCHAR(4000), l.f2_val) END AS f2_left,
        CASE WHEN (r.f2_val IS NULL AND NULLIF(LTRIM(RTRIM(r.f2_raw)), N'') IS NOT NULL) THEN r.f2_raw ELSE CONVERT(NVARCHAR(4000), r.f2_val) END AS f2_right,
        CAST(NULL AS DECIMAL(38,6)) AS f2_diff
    FROM (
        SELECT
            base.business_key,
            COUNT_BIG(*) AS key_rows,
            MAX(base.f1_val) AS f1_val,
            MAX(base.f1_raw) AS f1_raw,
            CONVERT(BIT, MAX(CONVERT(TINYINT, base.f2_val))) AS f2_val,
            MAX(base.f2_raw) AS f2_raw
        FROM (
            SELECT
                REPLACE(REPLACE(ISNULL(NULLIF(LTRIM(RTRIM(CONVERT(NVARCHAR(4000), s.[employee_id], 126) COLLATE Latin1_General_100_BIN2)), N''), N'∅'), '\', N'\\'), '|', N'\|') + N'|' + REPLACE(REPLACE(ISNULL(NULLIF(LTRIM(RTRIM(CONVERT(NVARCHAR(4000), s.[cost_center], 126) COLLATE Latin1_General_100_BIN2)), N''), N'∅'), '\', N'\\'), '|', N'\|') AS business_key,
                -- f1: annual_salary vs SALARY (int)
                TRY_CAST(CASE WHEN TRY_CAST(CASE WHEN LTRIM(RTRIM(CONVERT(NVARCHAR(4000), s.[annual_salary], 126))) = N'' THEN NULL ELSE s.[annual_salary] END AS DECIMAL(38,6)) % 1 = 0 THEN TRY_CAST(CASE WHEN LTRIM(RTRIM(CONVERT(NVARCHAR(4000), s.[annual_salary], 126))) = N'' THEN NULL ELSE s.[annual_salary] END AS DECIMAL(38,6)) END AS BIGINT) AS f1_val,
                CONVERT(NVARCHAR(4000), s.[annual_salary], 126) AS f1_raw,
                -- f2: active vs INACTIVE (bool)
                CAST(CASE WHEN UPPER(NULLIF(LTRIM(RTRIM(CONVERT(NVARCHAR(4000), s.[active], 126) COLLATE Latin1_General_100_BIN2)), N'')) IN (N'1', N'TRUE', N'YES', N'Y') THEN 1 WHEN UPPER(NULLIF(LTRIM(RTRIM(CONVERT(NVARCHAR(4000), s.[active], 126) COLLATE Latin1_General_100_BIN2)), N'')) IN (N'0', N'FALSE', N'NO', N'N') THEN 0 WHEN TRY_CAST(CASE WHEN LTRIM(RTRIM(CONVERT(NVARCHAR(4000), s.[active], 126))) = N'' THEN NULL ELSE s.[active] END AS DECIMAL(38,6)) = 1 THEN 1 WHEN TRY_CAST(CASE WHEN LTRIM(RTRIM(CONVERT(NVARCHAR(4000), s.[active], 126))) = N'' THEN NULL ELSE s.[active] END AS DECIMAL(38,6)) = 0 THEN 0 END AS BIT) AS f2_val,
                CONVERT(NVARCHAR(4000), s.[active], 126) AS f2_raw
            FROM (SELECT * FROM [stg_wd].[workers]) AS s
        ) AS base
        GROUP BY base.business_key
    ) AS l
    FULL OUTER JOIN (
        SELECT
            base.business_key,
            COUNT_BIG(*) AS key_rows,
            MAX(base.f1_val) AS f1_val,
            MAX(base.f1_raw) AS f1_raw,
            CONVERT(BIT, MAX(CONVERT(TINYINT, base.f2_val))) AS f2_val,
            MAX(base.f2_raw) AS f2_raw
        FROM (
            SELECT
                REPLACE(REPLACE(ISNULL(NULLIF(LTRIM(RTRIM(CONVERT(NVARCHAR(4000), s.[EMPLOYID], 126) COLLATE Latin1_General_100_BIN2)), N''), N'∅'), '\', N'\\'), '|', N'\|') + N'|' + REPLACE(REPLACE(ISNULL(NULLIF(LTRIM(RTRIM(CONVERT(NVARCHAR(4000), s.[DEPRTMNT], 126) COLLATE Latin1_General_100_BIN2)), N''), N'∅'), '\', N'\\'), '|', N'\|') AS business_key,
                -- f1: annual_salary vs SALARY (int)
                TRY_CAST(CASE WHEN TRY_CAST(CASE WHEN LTRIM(RTRIM(CONVERT(NVARCHAR(4000), s.[SALARY], 126))) = N'' THEN NULL ELSE s.[SALARY] END AS DECIMAL(38,6)) % 1 = 0 THEN TRY_CAST(CASE WHEN LTRIM(RTRIM(CONVERT(NVARCHAR(4000), s.[SALARY], 126))) = N'' THEN NULL ELSE s.[SALARY] END AS DECIMAL(38,6)) END AS BIGINT) AS f1_val,
                CONVERT(NVARCHAR(4000), s.[SALARY], 126) AS f1_raw,
                -- f2: active vs INACTIVE (bool)
                CAST(CASE WHEN UPPER(NULLIF(LTRIM(RTRIM(CONVERT(NVARCHAR(4000), s.[INACTIVE], 126) COLLATE Latin1_General_100_BIN2)), N'')) IN (N'1', N'TRUE', N'YES', N'Y') THEN 1 WHEN UPPER(NULLIF(LTRIM(RTRIM(CONVERT(NVARCHAR(4000), s.[INACTIVE], 126) COLLATE Latin1_General_100_BIN2)), N'')) IN (N'0', N'FALSE', N'NO', N'N') THEN 0 WHEN TRY_CAST(CASE WHEN LTRIM(RTRIM(CONVERT(NVARCHAR(4000), s.[INACTIVE], 126))) = N'' THEN NULL ELSE s.[INACTIVE] END AS DECIMAL(38,6)) = 1 THEN 1 WHEN TRY_CAST(CASE WHEN LTRIM(RTRIM(CONVERT(NVARCHAR(4000), s.[INACTIVE], 126))) = N'' THEN NULL ELSE s.[INACTIVE] END AS DECIMAL(38,6)) = 0 THEN 0 END AS BIT) AS f2_val,
                CONVERT(NVARCHAR(4000), s.[INACTIVE], 126) AS f2_raw
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
        (k.f1_break, N'annual_salary', k.f1_left, k.f1_right, k.f1_diff),
        (k.f2_break, N'active', k.f2_left, k.f2_right, k.f2_diff)
    ) AS f (break_type, field_name, left_value, right_value, diff)
    WHERE k.l_rows = 1 AND k.r_rows = 1 AND f.break_type IS NOT NULL
) AS b (break_type, field_name, left_value, right_value, diff)