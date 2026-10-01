-- Manual pages and automatic packets must not cancel each other's reservations.
ALTER TABLE page_capture_plans ADD COLUMN packet_owner TEXT;
ALTER TABLE page_capture_plans ADD COLUMN packet_revision TEXT;
