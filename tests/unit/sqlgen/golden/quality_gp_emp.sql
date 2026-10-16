DECLARE @period_start DATE = ?, @period_end_excl DATE = ?, @as_of DATE = ?;
SELECT
    (SELECT COUNT_BIG(*) FROM (SELECT * FROM [stg_gp].[UPR00100]) AS s) AS rows_left,
    CAST(0 AS BIGINT) AS rows_right
GO
DECLARE @period_start DATE = ?, @period_end_excl DATE = ?, @as_of DATE = ?;
SELECT
    CAST(q.business_key AS NVARCHAR(400)) COLLATE DATABASE_DEFAULT AS business_key,
    CAST(q.break_type AS VARCHAR(30)) AS break_type,
    CAST(q.field_name AS NVARCHAR(128)) AS field_name,
    CAST(q.left_value AS NVARCHAR(4000)) COLLATE DATABASE_DEFAULT AS left_value,
    CAST(q.right_value AS NVARCHAR(4000)) COLLATE DATABASE_DEFAULT AS right_value,
    CAST(q.diff AS DECIMAL(38,6)) AS diff
FROM (
    -- assertion 1
    SELECT
        REPLACE(REPLACE(ISNULL(UPPER(NULLIF(LTRIM(RTRIM(CONVERT(NVARCHAR(4000), s.[EMPLOYID], 126) COLLATE Latin1_General_100_BIN2)), N'')), N'∅'), '\', N'\\'), '|', N'\|') AS business_key,
        'quality_violation' AS break_type,
        v.field_name AS field_name,
        CAST(NULL AS NVARCHAR(4000)) AS left_value,
        CAST(NULL AS NVARCHAR(4000)) AS right_value,
        CAST(NULL AS DECIMAL(38,6)) AS diff
    FROM (SELECT * FROM [stg_gp].[UPR00100]) AS s
    CROSS APPLY (VALUES
        (N'EMPLOYID', UPPER(NULLIF(LTRIM(RTRIM(CONVERT(NVARCHAR(4000), s.[EMPLOYID], 126) COLLATE Latin1_General_100_BIN2)), N''))),
        (N'DEPRTMNT', UPPER(NULLIF(LTRIM(RTRIM(CONVERT(NVARCHAR(4000), s.[DEPRTMNT], 126) COLLATE Latin1_General_100_BIN2)), N'')))
    ) AS v (field_name, value)
    WHERE v.value IS NULL
    UNION ALL
    -- assertion 2
    SELECT
        REPLACE(REPLACE(ISNULL(u.k1, N'∅'), '\', N'\\'), '|', N'\|') AS business_key,
        'quality_violation' AS break_type,
        N'unique:EMPLOYID' AS field_name,
        CONVERT(NVARCHAR(4000), COUNT_BIG(*)) AS left_value,
        CAST(NULL AS NVARCHAR(4000)) AS right_value,
        CAST(NULL AS DECIMAL(38,6)) AS diff
    FROM (
        SELECT
            UPPER(NULLIF(LTRIM(RTRIM(CONVERT(NVARCHAR(4000), s.[EMPLOYID], 126) COLLATE Latin1_General_100_BIN2)), N'')) AS k1
        FROM (SELECT * FROM [stg_gp].[UPR00100]) AS s
    ) AS u
    WHERE u.k1 IS NOT NULL
    GROUP BY u.k1
    HAVING COUNT_BIG(*) > 1
    UNION ALL
    -- assertion 3
    SELECT
        REPLACE(REPLACE(ISNULL(UPPER(NULLIF(LTRIM(RTRIM(CONVERT(NVARCHAR(4000), s.[EMPLOYID], 126) COLLATE Latin1_General_100_BIN2)), N'')), N'∅'), '\', N'\\'), '|', N'\|') AS business_key,
        'quality_violation' AS break_type,
        N'INACTIVE' AS field_name,
        CONVERT(NVARCHAR(4000), s.[INACTIVE], 126) AS left_value,
        CAST(NULL AS NVARCHAR(4000)) AS right_value,
        CAST(NULL AS DECIMAL(38,6)) AS diff
    FROM (SELECT * FROM [stg_gp].[UPR00100]) AS s
    WHERE UPPER(NULLIF(LTRIM(RTRIM(CONVERT(NVARCHAR(4000), s.[INACTIVE], 126) COLLATE Latin1_General_100_BIN2)), N'')) IS NOT NULL
        AND NOT EXISTS (
            SELECT 1 FROM (VALUES
                (UPPER(NULLIF(LTRIM(RTRIM(CONVERT(NVARCHAR(4000), N'0', 126) COLLATE Latin1_General_100_BIN2)), N''))),
                (UPPER(NULLIF(LTRIM(RTRIM(CONVERT(NVARCHAR(4000), N'1', 126) COLLATE Latin1_General_100_BIN2)), N'')))
            ) AS a (value)
            WHERE a.value = UPPER(NULLIF(LTRIM(RTRIM(CONVERT(NVARCHAR(4000), s.[INACTIVE], 126) COLLATE Latin1_General_100_BIN2)), N''))
        )
    UNION ALL
    -- assertion 4
    SELECT
        REPLACE(REPLACE(ISNULL(UPPER(NULLIF(LTRIM(RTRIM(CONVERT(NVARCHAR(4000), s.[EMPLOYID], 126) COLLATE Latin1_General_100_BIN2)), N'')), N'∅'), '\', N'\\'), '|', N'\|') AS business_key,
        'quality_violation' AS break_type,
        N'DEPRTMNT' AS field_name,
        CONVERT(NVARCHAR(4000), s.[DEPRTMNT], 126) AS left_value,
        CAST(NULL AS NVARCHAR(4000)) AS right_value,
        CAST(NULL AS DECIMAL(38,6)) AS diff
    FROM (SELECT * FROM [stg_gp].[UPR00100]) AS s
    WHERE UPPER(NULLIF(LTRIM(RTRIM(CONVERT(NVARCHAR(4000), s.[DEPRTMNT], 126) COLLATE Latin1_General_100_BIN2)), N'')) IS NOT NULL
        AND NOT EXISTS (
            SELECT 1 FROM (SELECT * FROM [stg_gp].[UPR40300]) AS r WHERE UPPER(NULLIF(LTRIM(RTRIM(CONVERT(NVARCHAR(4000), r.[DEPRTMNT], 126) COLLATE Latin1_General_100_BIN2)), N'')) = UPPER(NULLIF(LTRIM(RTRIM(CONVERT(NVARCHAR(4000), s.[DEPRTMNT], 126) COLLATE Latin1_General_100_BIN2)), N''))
        )
    UNION ALL
    -- assertion 5
    SELECT
        REPLACE(REPLACE(ISNULL(UPPER(NULLIF(LTRIM(RTRIM(CONVERT(NVARCHAR(4000), s.[EMPLOYID], 126) COLLATE Latin1_General_100_BIN2)), N'')), N'∅'), '\', N'\\'), '|', N'\|') AS business_key,
        'quality_violation' AS break_type,
        N'EMPLOYID' AS field_name,
        CONVERT(NVARCHAR(4000), s.[EMPLOYID], 126) AS left_value,
        CAST(NULL AS NVARCHAR(4000)) AS right_value,
        CAST(NULL AS DECIMAL(38,6)) AS diff
    FROM (SELECT * FROM [stg_gp].[UPR00100]) AS s
    WHERE UPPER(NULLIF(LTRIM(RTRIM(CONVERT(NVARCHAR(4000), s.[EMPLOYID], 126) COLLATE Latin1_General_100_BIN2)), N'')) IS NOT NULL
        AND CONVERT(NVARCHAR(4000), s.[EMPLOYID], 126) COLLATE Latin1_General_100_BIN2 NOT LIKE N'[0-9][0-9][0-9][0-9][0-9][0-9]' COLLATE Latin1_General_100_BIN2
    UNION ALL
    -- assertion 6
    SELECT
        CONVERT(NVARCHAR(400), c.business_key) AS business_key,
        'quality_violation' AS break_type,
        N'future_hire_dates' AS field_name,
        CAST(NULL AS NVARCHAR(4000)) AS left_value,
        CAST(NULL AS NVARCHAR(4000)) AS right_value,
        CAST(NULL AS DECIMAL(38,6)) AS diff
    FROM (
        SELECT EMPLOYID AS business_key FROM (SELECT * FROM [stg_gp].[UPR00100]) AS [dataset] WHERE STRTDATE > GETDATE()
    ) AS c
) AS q