CREATE SCHEMA [probe];
GO
CREATE TYPE [probe].[Code] FROM varchar(10) NOT NULL;
GO
CREATE TABLE [probe].[Parent] (
    [ParentId] int IDENTITY(1, 1) NOT NULL,
    [Code] [probe].[Code],
    CONSTRAINT [PK_Parent] PRIMARY KEY CLUSTERED ([ParentId]),
    CONSTRAINT [UQ_Parent_Code] UNIQUE NONCLUSTERED ([Code])
);
GO
CREATE TABLE [probe].[Order] (
    [OrderId] bigint IDENTITY(100, 5) NOT NULL,
    [ParentId] int NOT NULL,
    [Line] smallint NOT NULL,
    [Name] nvarchar(100) COLLATE Latin1_General_CS_AS NULL,
    [Notes] nvarchar(max) NULL,
    [Blob] varbinary(max) NULL,
    [Amount] decimal(18, 2) NOT NULL CONSTRAINT [DF_Order_Amount] DEFAULT (0),
    [Rate] numeric(9, 4) NULL DEFAULT ((1.5)),
    [Flag] bit NOT NULL CONSTRAINT [DF_Order_Flag] DEFAULT ((0)),
    [CreatedAt] datetime2(3) NOT NULL CONSTRAINT [DF_Order_CreatedAt] DEFAULT (sysutcdatetime()),
    [At] datetimeoffset(7) NULL,
    [T] time(0) NULL,
    [Price] money NULL,
    [Ch] char(3) NULL,
    [NCh] nchar(2) NULL,
    [Bin] binary(16) NULL,
    [Guid] uniqueidentifier NOT NULL CONSTRAINT [DF_Order_Guid] DEFAULT (newid()),
    [Total] AS ([Amount] * [Line]) PERSISTED NOT NULL,
    [Label] AS (concat([Name], N'-', [Line])),
    [select] int NULL,
    [Has Space] varchar(20) NULL,
    [Ver] rowversion NOT NULL,
    [F] float NULL,
    [R] real NULL,
    [D] date NULL,
    CONSTRAINT [PK_Order] PRIMARY KEY CLUSTERED ([OrderId] ASC, [Line] DESC),
    CONSTRAINT [FK_Order_Parent] FOREIGN KEY ([ParentId]) REFERENCES [probe].[Parent] ([ParentId]) ON DELETE CASCADE,
    CONSTRAINT [CK_Order_Amount] CHECK ([Amount] >= 0),
    CHECK ([Line] > 0),
    UNIQUE NONCLUSTERED ([Guid])
);
GO
CREATE NONCLUSTERED INDEX [IX_Order_Parent] ON [probe].[Order] ([ParentId], [CreatedAt] DESC) INCLUDE ([Amount], [Name]) WHERE ([Flag] = 1);
GO
CREATE UNIQUE NONCLUSTERED INDEX [UX_Order_Name] ON [probe].[Order] ([Name]);
GO
CREATE TRIGGER [probe].[trOrder_Touch] ON [probe].[Order] AFTER UPDATE AS
BEGIN
    SET NOCOUNT ON;
END
GO
CREATE TABLE [probe].[Plain] (
    [Id] int NOT NULL PRIMARY KEY,
    [Value] varchar(50) NULL
);
GO
CREATE TABLE [probe].[Heap] (
    [Only] int NULL
);
GO
CREATE TABLE [probe].[Wide] (
    [Id] int NOT NULL,
    [Payload] nvarchar(4000) NULL
);
GO
CREATE CLUSTERED COLUMNSTORE INDEX [CCI_Wide] ON [probe].[Wide];
GO
