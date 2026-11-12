-- Create role without login
create role app_write_role nologin;

-- insert password from env
create role encoder login password :'encoder_pw' in role app_write_role;

-- Allow the role to look up objects in the schema
grant usage on schema public to app_write_role;

-- Explicitly ensure the role cannot create new tables/objects
revoke create on schema public to app_write_role;

-- Grant read, insert, and update permissions on a specific table
grant select,
    insert,
    update on 
        invoices,
        vendors,
        line_items to app_write_role;

-- caps runaway queries such as SELECT pg_sleep(...)
ALTER ROLE encoder SET statement_timeout = '5s';
