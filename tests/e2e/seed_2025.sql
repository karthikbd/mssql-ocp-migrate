-- SQL Server 2025 only (run when the source is 17.x): json and vector columns
USE e2e_src;
GO
CREATE TABLE dbo.Embedding (
    Id     int NOT NULL PRIMARY KEY,
    Doc    json NULL,
    Vec    vector(3) NULL
);
INSERT dbo.Embedding VALUES
    (1, '{"a": 1, "tags": ["x", "y"]}', '[0.1, 0.2, 0.3]'),
    (2, NULL, NULL),
    (3, '{"unicode": "Grüße 漢字"}', '[1, -2.5, 3e-5]');
GO
