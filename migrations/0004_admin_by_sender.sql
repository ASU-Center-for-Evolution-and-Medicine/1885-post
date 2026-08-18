-- Admin scope moves from to_address (the shared inbox -- same for every newsletter in a
-- single-inbox archive, so never actually useful as a grant axis) to from_email (who
-- actually sent the newsletter, which is what varies and what "own your newsletter"
-- should mean).

ALTER TABLE newsletters ADD COLUMN from_email TEXT;
CREATE INDEX IF NOT EXISTS idx_newsletters_from_email ON newsletters(from_email);

-- Backfill from the existing from_address ("Display Name <email>" or a bare email).
UPDATE newsletters
SET from_email = CASE
  WHEN from_address LIKE '%<%>%'
    THEN substr(from_address, instr(from_address, '<') + 1, instr(from_address, '>') - instr(from_address, '<') - 1)
  ELSE from_address
END;

-- Old to_address-scoped grants don't map to any real from_email -- start clean, the
-- admin dashboard makes re-granting quick.
DELETE FROM newsletter_admins;
ALTER TABLE newsletter_admins RENAME COLUMN to_address TO from_email;
