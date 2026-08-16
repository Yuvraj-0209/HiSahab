-- Runs once, on first container start, as the postgres superuser.
-- The test suite needs its own database so a test run can never touch dev data.
CREATE DATABASE hisahab_test OWNER hisahab;
