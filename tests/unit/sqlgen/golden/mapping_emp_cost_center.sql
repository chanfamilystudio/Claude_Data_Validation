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
    (SELECT COUNT_BIG(DISTINCT UPPER(NULLIF(LTRIM(RTRIM(CONVERT(NVARCHAR(4000), s.[cost_center], 126) COLLATE Latin1_General_100_BIN2)), N''))) FROM (SELECT * FROM [stg_wd].[workers]) AS s) AS rows_left,
    (SELECT COUNT_BIG(DISTINCT UPPER(NULLIF(LTRIM(RTRIM(CONVERT(NVARCHAR(4000), s.[DEPRTMNT], 126) COLLATE Latin1_General_100_BIN2)), N''))) FROM (SELECT * FROM [stg_gp].[UPR40300]) AS s) AS rows_right
GO
DECLARE @period_start DATE = ?, @period_end_excl DATE = ?, @as_of DATE = ?;
SELECT
    CAST(REPLACE(REPLACE(ISNULL(x.src_value, N'∅'), '\', N'\\'), '|', N'\|') AS NVARCHAR(400)) COLLATE DATABASE_DEFAULT AS business_key,
    CAST(x.break_type AS VARCHAR(30)) AS break_type,
    CAST(N'cost_center' AS NVARCHAR(128)) AS field_name,
    CAST(x.src_value AS NVARCHAR(4000)) COLLATE DATABASE_DEFAULT AS left_value,
    CAST(x.mapped_value AS NVARCHAR(4000)) COLLATE DATABASE_DEFAULT AS right_value,
    CAST(NULL AS DECIMAL(38,6)) AS diff
FROM (
    SELECT
        sv.src_value,
        mp.to_value AS mapped_value,
        CASE
            WHEN mp.from_value IS NULL THEN 'mapping_missing'
            WHEN mp.to_value IS NULL OR NOT EXISTS (
                SELECT 1 FROM (SELECT * FROM [stg_gp].[UPR40300]) AS t WHERE UPPER(NULLIF(LTRIM(RTRIM(CONVERT(NVARCHAR(4000), t.[DEPRTMNT], 126) COLLATE Latin1_General_100_BIN2)), N'')) = mp.to_value
            ) THEN 'mapping_mismatch'
        END AS break_type
    FROM (
        SELECT DISTINCT UPPER(NULLIF(LTRIM(RTRIM(CONVERT(NVARCHAR(4000), s.[cost_center], 126) COLLATE Latin1_General_100_BIN2)), N'')) AS src_value
        FROM (SELECT * FROM [stg_wd].[workers]) AS s
    ) AS sv
    LEFT JOIN (
        SELECT UPPER(NULLIF(LTRIM(RTRIM(CONVERT(NVARCHAR(4000), m.[wd_cost_center], 126) COLLATE Latin1_General_100_BIN2)), N'')) AS from_value, UPPER(NULLIF(LTRIM(RTRIM(CONVERT(NVARCHAR(4000), m.[gp_dept], 126) COLLATE Latin1_General_100_BIN2)), N'')) AS to_value
        FROM (SELECT * FROM [xref].[cost_center_map] WHERE [eff_start] <= @as_of AND ([eff_end] IS NULL OR [eff_end] >= @as_of)) AS m
    ) AS mp
        ON mp.from_value = sv.src_value
    WHERE sv.src_value IS NOT NULL
) AS x
WHERE x.break_type IS NOT NULL