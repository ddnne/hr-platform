-- The public listing defaults to a moving time-of-day filter even for past dates.
-- Revisit pages with the explicit 00:00 filter. Existing attempts and raw objects
-- remain unchanged; only the page completion cache is invalidated once.
UPDATE archive_jobs SET status='PENDING' WHERE status='DONE';
