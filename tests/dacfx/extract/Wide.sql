CREATE TABLE [probe].[Wide] (
    [Id]      INT             NOT NULL,
    [Payload] NVARCHAR (4000) NULL
);


GO

CREATE CLUSTERED COLUMNSTORE INDEX [CCI_Wide]
    ON [probe].[Wide];


GO

