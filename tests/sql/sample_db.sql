-- A small database with one of each kind of object, used by the integration tests.
CREATE SCHEMA sales;
GO
CREATE TABLE sales.Customer
(
      CustomerId int IDENTITY(1, 1) NOT NULL CONSTRAINT PK_Customer PRIMARY KEY
     ,Name nvarchar(100) NOT NULL
     ,Email varchar(320) NULL CONSTRAINT UQ_Customer_Email UNIQUE
     ,Status char(1) NOT NULL CONSTRAINT DF_Customer_Status DEFAULT ('A')
     ,CreatedAt datetime2(3) NOT NULL CONSTRAINT DF_Customer_CreatedAt DEFAULT (sysutcdatetime())
     ,CreditLimit decimal(19, 4) NULL
     ,Notes nvarchar(max) COLLATE Latin1_General_100_CI_AS NULL
     ,CONSTRAINT CK_Customer_Status CHECK (Status IN ('A', 'I'))
);
GO
CREATE TABLE sales.[Order]
(
      OrderId bigint IDENTITY(1000, 1) NOT NULL
     ,CustomerId int NOT NULL
     ,OrderDate date NOT NULL
     ,Amount decimal(19, 4) NOT NULL
     ,Tax AS (Amount * 0.1) PERSISTED
     ,CONSTRAINT PK_Order PRIMARY KEY CLUSTERED (OrderId)
     ,CONSTRAINT FK_Order_Customer FOREIGN KEY (CustomerId) REFERENCES sales.Customer (CustomerId) ON DELETE CASCADE
);
GO
CREATE NONCLUSTERED INDEX IX_Order_Customer ON sales.[Order] (CustomerId, OrderDate DESC) INCLUDE (Amount);
GO
CREATE INDEX IX_Customer_Active ON sales.Customer (Name) WHERE Status = 'A';
GO
CREATE VIEW sales.vCustomerTotals
AS
SELECT
      c.CustomerId
     ,c.Name
     ,SUM(o.Amount) AS Total
FROM sales.Customer AS c
INNER JOIN sales.[Order] AS o
    ON o.CustomerId = c.CustomerId
GROUP BY c.CustomerId, c.Name;
GO
CREATE FUNCTION dbo.fn_Double (@x int)
RETURNS int
AS
BEGIN
    RETURN @x * 2;
END;
GO
CREATE PROCEDURE sales.usp_GetCustomer @CustomerId int
AS
BEGIN
    SET NOCOUNT ON;
    SELECT * FROM sales.Customer WHERE CustomerId = @CustomerId;
END;
GO
CREATE TRIGGER sales.trCustomer_Touch ON sales.Customer AFTER UPDATE
AS
BEGIN
    SET NOCOUNT ON;
END;
GO
-- Constraints SQL Server names itself (PK__Tag__..., DF__Tag__...): those names differ in every
-- database, so the scripts must leave them out.
CREATE TABLE dbo.Tag
(
      TagId int NOT NULL PRIMARY KEY
     ,Label varchar(50) NOT NULL DEFAULT ('')
     ,CustomerId int NULL REFERENCES sales.Customer (CustomerId)
     ,CHECK (LEN(Label) < 50)
);
GO
