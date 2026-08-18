-- Visibility is now open to every authenticated user (Access already gates who reaches
-- the site at all). What email_access actually grants now is admin rights over a
-- specific to_address's newsletters -- currently just "can delete them" -- so rename it
-- to say what it means.
ALTER TABLE email_access RENAME TO newsletter_admins;
