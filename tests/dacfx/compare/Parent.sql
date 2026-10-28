CREATE TABLE [probe].[Parent] (
    [ParentId] INT            IDENTITY (1, 1) NOT NULL,
    [Code]     [probe].[Code] NOT NULL,
    CONSTRAINT [PK_Parent] PRIMARY KEY CLUSTERED ([ParentId] ASC),
    CONSTRAINT [UQ_Parent_Code] UNIQUE NONCLUSTERED ([Code] ASC)
);
GO

