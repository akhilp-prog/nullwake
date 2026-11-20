-- Separate database for the optional demo warehouse. Unused unless the demo profile runs.
CREATE DATABASE warehouse;
-- Only the demo's read-only role (granted by demo/simulator.py init) may connect; nothing else on this server can.
REVOKE CONNECT ON DATABASE warehouse FROM PUBLIC;
