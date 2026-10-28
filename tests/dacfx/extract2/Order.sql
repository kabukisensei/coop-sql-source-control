CREATE TABLE [probe].[Order] (
    [OrderId]   BIGINT             IDENTITY (100, 5) NOT NULL,
    [ParentId]  INT                NOT NULL,
    [Line]      SMALLINT           NOT NULL,
    [Name]      NVARCHAR (100)     COLLATE Latin1_General_CS_AS NULL,
    [Notes]     NVARCHAR (MAX)     NULL,
    [Blob]      VARBINARY (MAX)    NULL,
    [Amount]    DECIMAL (18, 2)    CONSTRAINT [DF_Order_Amount] DEFAULT ((0)) NOT NULL,
    [Rate]      NUMERIC (9, 4)     DEFAULT ((1.5)) NULL,
    [Flag]      BIT                CONSTRAINT [DF_Order_Flag] DEFAULT ((0)) NOT NULL,
    [CreatedAt] DATETIME2 (3)      CONSTRAINT [DF_Order_CreatedAt] DEFAULT (sysutcdatetime()) NOT NULL,
    [At]        DATETIMEOFFSET (7) NULL,
    [T]         TIME (0)           NULL,
    [Price]     MONEY              NULL,
    [Ch]        CHAR (3)           NULL,
    [NCh]       NCHAR (2)          NULL,
    [Bin]       BINARY (16)        NULL,
    [Guid]      UNIQUEIDENTIFIER   CONSTRAINT [DF_Order_Guid] DEFAULT (newid()) NOT NULL,
    [Total]     AS                 ([Amount]*[Line]) PERSISTED NOT NULL,
    [Label]     AS                 (concat([Name],N'-',[Line])),
    [select]    INT                NULL,
    [Has Space] VARCHAR (20)       NULL,
    [Ver]       ROWVERSION         NOT NULL,
    [F]         FLOAT (53)         NULL,
    [R]         REAL               NULL,
    [D]         DATE               NULL,
    [Extra]     INT                NULL,
    CONSTRAINT [PK_Order] PRIMARY KEY CLUSTERED ([OrderId] ASC, [Line] DESC),
    CHECK ([Line]>(0)),
    CONSTRAINT [CK_Order_Amount] CHECK ([Amount]>=(0)),
    CONSTRAINT [CK_Order_Line2] CHECK ([Line]<(1000)),
    CONSTRAINT [FK_Order_Parent] FOREIGN KEY ([ParentId]) REFERENCES [probe].[Parent] ([ParentId]) ON DELETE CASCADE,
    UNIQUE NONCLUSTERED ([Guid] ASC)
);


GO
ALTER TABLE [probe].[Order] NOCHECK CONSTRAINT [FK_Order_Parent];


GO

CREATE NONCLUSTERED INDEX [IX_Order_Parent]
    ON [probe].[Order]([ParentId] ASC, [CreatedAt] DESC)
    INCLUDE([Amount], [Name]) WHERE ([Flag]=(1));


GO

CREATE UNIQUE NONCLUSTERED INDEX [UX_Order_Name]
    ON [probe].[Order]([Name] ASC);


GO


CREATE TRIGGER [probe].[trOrder_Touch] ON [probe].[Order] AFTER UPDATE AS
BEGIN
    SET NOCOUNT ON;
END

GO

