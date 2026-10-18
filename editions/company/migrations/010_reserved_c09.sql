-- Reserved migration slot for C09 (minimal management console).
-- C10 is fixed at version 011 so the p0 phase order stays stable while the
-- remaining phases land independently. This file intentionally changes no schema.
DO $migration$
BEGIN
  NULL;
END;
$migration$;
