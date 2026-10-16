-- V001: initial `recon` schema (design spec section 3).
-- recon.schema_version is created by the migration runner before any file is applied.
-- Statements are separated by GO lines; each file runs in one transaction.

IF SCHEMA_ID('recon') IS NULL EXEC('CREATE SCHEMA recon');
GO

CREATE TABLE recon.[rule] (
    rule_id       VARCHAR(100)   NOT NULL,
    version_hash  CHAR(64)       NOT NULL,
    domain        VARCHAR(100)   NOT NULL,
    check_type    VARCHAR(50)    NOT NULL,
    severity      VARCHAR(10)    NOT NULL,
    owner         NVARCHAR(256)  NULL,
    yaml_text     NVARCHAR(MAX)  NOT NULL,
    active        BIT            NOT NULL CONSTRAINT DF_rule_active DEFAULT 1,
    loaded_at     DATETIME2(3)   NOT NULL CONSTRAINT DF_rule_loaded_at DEFAULT SYSUTCDATETIME(),
    CONSTRAINT PK_rule PRIMARY KEY (rule_id, version_hash),
    CONSTRAINT CK_rule_severity CHECK (severity IN ('high', 'medium', 'low'))
);
GO

CREATE TABLE recon.run (
    run_id          BIGINT IDENTITY(1,1) NOT NULL CONSTRAINT PK_run PRIMARY KEY,
    [trigger]       VARCHAR(20)    NOT NULL,
    domain          VARCHAR(100)   NULL,
    period_label    VARCHAR(20)    NOT NULL,
    period_start    DATE           NOT NULL,
    period_end      DATE           NOT NULL,
    status          VARCHAR(20)    NOT NULL,
    run_by          NVARCHAR(256)  NOT NULL,
    git_commit      VARCHAR(64)    NULL,
    engine_version  VARCHAR(40)    NOT NULL,
    started_at      DATETIME2(3)   NOT NULL CONSTRAINT DF_run_started_at DEFAULT SYSUTCDATETIME(),
    finished_at     DATETIME2(3)   NULL,
    CONSTRAINT CK_run_trigger CHECK ([trigger] IN ('scheduled', 'manual')),
    CONSTRAINT CK_run_status CHECK (status IN ('running', 'succeeded', 'partial', 'failed'))
);
CREATE INDEX IX_run_period ON recon.run (domain, period_label, run_id);
GO

CREATE TABLE recon.run_check (
    run_check_id   BIGINT IDENTITY(1,1) NOT NULL CONSTRAINT PK_run_check PRIMARY KEY,
    run_id         BIGINT         NOT NULL CONSTRAINT FK_run_check_run REFERENCES recon.run (run_id),
    rule_id        VARCHAR(100)   NOT NULL,
    version_hash   CHAR(64)       NOT NULL,
    result         VARCHAR(10)    NOT NULL,
    rows_left      BIGINT         NULL,
    rows_right     BIGINT         NULL,
    break_count    INT            NOT NULL CONSTRAINT DF_run_check_break_count DEFAULT 0,
    generated_sql  NVARCHAR(MAX)  NULL,
    duration_ms    INT            NULL,
    error_message  NVARCHAR(MAX)  NULL,
    CONSTRAINT FK_run_check_rule FOREIGN KEY (rule_id, version_hash) REFERENCES recon.[rule] (rule_id, version_hash),
    CONSTRAINT CK_run_check_result CHECK (result IN ('pass', 'fail', 'error'))
);
CREATE INDEX IX_run_check_run ON recon.run_check (run_id);
GO

CREATE TABLE recon.total_result (
    total_result_id    BIGINT IDENTITY(1,1) NOT NULL CONSTRAINT PK_total_result PRIMARY KEY,
    run_check_id       BIGINT         NOT NULL CONSTRAINT FK_total_result_run_check REFERENCES recon.run_check (run_check_id),
    group_key          NVARCHAR(400)  NULL,
    measure            NVARCHAR(128)  NOT NULL,
    left_value         DECIMAL(38,6)  NULL,
    right_value        DECIMAL(38,6)  NULL,
    diff               DECIMAL(38,6)  NULL,
    tolerance_applied  DECIMAL(38,6)  NULL,
    within_tolerance   BIT            NOT NULL
);
CREATE INDEX IX_total_result_run_check ON recon.total_result (run_check_id);
GO

CREATE TABLE recon.import_batch (
    import_batch_id  BIGINT IDENTITY(1,1) NOT NULL CONSTRAINT PK_import_batch PRIMARY KEY,
    file_name        NVARCHAR(260)  NOT NULL,
    file_sha256      CHAR(64)       NOT NULL CONSTRAINT UQ_import_batch_sha UNIQUE,
    imported_by      NVARCHAR(256)  NOT NULL,
    rows_applied     INT            NOT NULL CONSTRAINT DF_import_batch_applied DEFAULT 0,
    rows_rejected    INT            NOT NULL CONSTRAINT DF_import_batch_rejected DEFAULT 0,
    imported_at      DATETIME2(3)   NOT NULL CONSTRAINT DF_import_batch_at DEFAULT SYSUTCDATETIME()
);
GO

CREATE TABLE recon.[break] (
    break_id          BIGINT IDENTITY(1,1) NOT NULL CONSTRAINT PK_break PRIMARY KEY,
    break_key         CHAR(64)       NOT NULL CONSTRAINT UQ_break_key UNIQUE,
    rule_id           VARCHAR(100)   NOT NULL,
    domain            VARCHAR(100)   NOT NULL,
    period_label      VARCHAR(20)    NOT NULL,
    business_key      NVARCHAR(400)  NOT NULL,
    break_type        VARCHAR(30)    NOT NULL,
    field_name        NVARCHAR(128)  NULL,
    left_value        NVARCHAR(4000) NULL,
    right_value       NVARCHAR(4000) NULL,
    diff              DECIMAL(38,6)  NULL,
    status            VARCHAR(20)    NOT NULL CONSTRAINT DF_break_status DEFAULT 'open',
    owner             NVARCHAR(256)  NULL,
    resolution_type   VARCHAR(20)    NULL,
    post_signoff      BIT            NOT NULL CONSTRAINT DF_break_post_signoff DEFAULT 0,
    first_seen_run_id BIGINT         NOT NULL CONSTRAINT FK_break_first_run REFERENCES recon.run (run_id),
    last_seen_run_id  BIGINT         NOT NULL CONSTRAINT FK_break_last_run REFERENCES recon.run (run_id),
    resolved_run_id   BIGINT         NULL CONSTRAINT FK_break_resolved_run REFERENCES recon.run (run_id),
    version           INT            NOT NULL CONSTRAINT DF_break_version DEFAULT 1,
    created_at        DATETIME2(3)   NOT NULL CONSTRAINT DF_break_created_at DEFAULT SYSUTCDATETIME(),
    updated_at        DATETIME2(3)   NOT NULL CONSTRAINT DF_break_updated_at DEFAULT SYSUTCDATETIME(),
    CONSTRAINT CK_break_status CHECK (status IN ('open', 'investigating', 'resolved', 'accepted')),
    CONSTRAINT CK_break_type CHECK (break_type IN (
        'missing_left', 'missing_right', 'value_mismatch', 'mapping_missing',
        'mapping_mismatch', 'quality_violation', 'total_mismatch')),
    CONSTRAINT CK_break_resolution CHECK (resolution_type IS NULL OR resolution_type IN ('auto_cleared', 'manual'))
);
CREATE INDEX IX_break_rule_period ON recon.[break] (rule_id, period_label, status);
CREATE INDEX IX_break_domain_period ON recon.[break] (domain, period_label, status);
GO

CREATE TABLE recon.break_occurrence (
    break_id     BIGINT         NOT NULL CONSTRAINT FK_break_occurrence_break REFERENCES recon.[break] (break_id),
    run_id       BIGINT         NOT NULL CONSTRAINT FK_break_occurrence_run REFERENCES recon.run (run_id),
    left_value   NVARCHAR(4000) NULL,
    right_value  NVARCHAR(4000) NULL,
    diff         DECIMAL(38,6)  NULL,
    CONSTRAINT PK_break_occurrence PRIMARY KEY (break_id, run_id)
);
CREATE INDEX IX_break_occurrence_run ON recon.break_occurrence (run_id);
GO

CREATE TABLE recon.break_comment (
    comment_id       BIGINT IDENTITY(1,1) NOT NULL CONSTRAINT PK_break_comment PRIMARY KEY,
    break_id         BIGINT         NOT NULL CONSTRAINT FK_break_comment_break REFERENCES recon.[break] (break_id),
    [text]           NVARCHAR(MAX)  NOT NULL,
    author           NVARCHAR(256)  NOT NULL,
    created_at       DATETIME2(3)   NOT NULL CONSTRAINT DF_break_comment_created DEFAULT SYSUTCDATETIME(),
    import_batch_id  BIGINT         NULL CONSTRAINT FK_break_comment_batch REFERENCES recon.import_batch (import_batch_id)
);
CREATE INDEX IX_break_comment_break ON recon.break_comment (break_id);
GO

CREATE TABLE recon.audit_log (
    audit_id         BIGINT IDENTITY(1,1) NOT NULL CONSTRAINT PK_audit_log PRIMARY KEY,
    entity           VARCHAR(20)    NOT NULL,
    entity_id        BIGINT         NOT NULL,
    field            NVARCHAR(128)  NOT NULL,
    old_value        NVARCHAR(4000) NULL,
    new_value        NVARCHAR(4000) NULL,
    changed_by       NVARCHAR(256)  NOT NULL,
    changed_at       DATETIME2(3)   NOT NULL CONSTRAINT DF_audit_log_changed DEFAULT SYSUTCDATETIME(),
    source           VARCHAR(10)    NOT NULL,
    run_id           BIGINT         NULL CONSTRAINT FK_audit_log_run REFERENCES recon.run (run_id),
    import_batch_id  BIGINT         NULL CONSTRAINT FK_audit_log_batch REFERENCES recon.import_batch (import_batch_id),
    CONSTRAINT CK_audit_log_entity CHECK (entity IN ('break', 'signoff')),
    CONSTRAINT CK_audit_log_source CHECK (source IN ('engine', 'import', 'cli'))
);
CREATE INDEX IX_audit_log_entity ON recon.audit_log (entity, entity_id);
GO

CREATE TABLE recon.signoff (
    signoff_id            BIGINT IDENTITY(1,1) NOT NULL CONSTRAINT PK_signoff PRIMARY KEY,
    domain                VARCHAR(100)   NOT NULL,
    period_label          VARCHAR(20)    NOT NULL,
    run_id                BIGINT         NOT NULL CONSTRAINT FK_signoff_run REFERENCES recon.run (run_id),
    signed_by             NVARCHAR(256)  NOT NULL,
    signed_at             DATETIME2(3)   NOT NULL CONSTRAINT DF_signoff_signed_at DEFAULT SYSUTCDATETIME(),
    comment               NVARCHAR(MAX)  NULL,
    open_break_count      INT            NOT NULL CONSTRAINT DF_signoff_open DEFAULT 0,
    accepted_break_count  INT            NOT NULL CONSTRAINT DF_signoff_accepted DEFAULT 0,
    evidence_path         NVARCHAR(500)  NULL,
    evidence_sha256       CHAR(64)       NULL,
    status                VARCHAR(10)    NOT NULL CONSTRAINT DF_signoff_status DEFAULT 'signed',
    revoked_by            NVARCHAR(256)  NULL,
    revoked_at            DATETIME2(3)   NULL,
    revoke_reason         NVARCHAR(MAX)  NULL,
    CONSTRAINT CK_signoff_status CHECK (status IN ('signed', 'revoked')),
    CONSTRAINT CK_signoff_open_zero CHECK (open_break_count = 0)
);
CREATE UNIQUE INDEX UX_signoff_one_signed ON recon.signoff (domain, period_label) WHERE status = 'signed';
GO

-- Roles (spec 3.3). Staging/mapping SELECT grants for recon_engine are site-specific and
-- documented in docs/operations.md rather than hard-coded here. EXECUTE on sp_getapplock
-- needs no grant: system procedures are executable by `public`.
IF DATABASE_PRINCIPAL_ID('recon_engine') IS NULL CREATE ROLE recon_engine;
GO
IF DATABASE_PRINCIPAL_ID('recon_reader') IS NULL CREATE ROLE recon_reader;
GO
GRANT SELECT, INSERT, UPDATE, DELETE ON SCHEMA::recon TO recon_engine;
GRANT SELECT ON SCHEMA::recon TO recon_reader;
GO
