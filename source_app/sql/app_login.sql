-- Least-privilege login for the web app and the pipeline (run once as an administrator, e.g. in SSMS).
-- The app can read and write rows in ShopDB but cannot create, alter or drop anything, cannot touch other
-- databases, and is not a server admin. Schema scripts (setup_db.py, migrate_*.py) keep using an admin login.
--
-- 1. Replace the password below with a long random one (never commit it).
-- 2. SQL Server must allow SQL logins: Server Properties > Security > "SQL Server and Windows Authentication mode",
--    then restart the SQL Server service.
-- 3. Put SQL_USER=raunaqi_app and SQL_PASSWORD=<the password> in .env and restart the app.

USE master;
IF NOT EXISTS (SELECT 1 FROM sys.server_principals WHERE name = 'raunaqi_app')
    CREATE LOGIN raunaqi_app WITH PASSWORD = 'CHANGE-ME-to-a-long-random-password', CHECK_POLICY = ON;
GO
USE ShopDB;
IF NOT EXISTS (SELECT 1 FROM sys.database_principals WHERE name = 'raunaqi_app')
    CREATE USER raunaqi_app FOR LOGIN raunaqi_app WITH DEFAULT_SCHEMA = dbo;
GRANT SELECT, INSERT, UPDATE, DELETE ON SCHEMA::dbo TO raunaqi_app;
GRANT VIEW CHANGE TRACKING ON SCHEMA::dbo TO raunaqi_app;   -- the pipeline's change detection
GO
