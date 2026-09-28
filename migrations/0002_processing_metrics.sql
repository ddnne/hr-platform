-- Wall time of the successful invocation through raw/index publication, excluding this metric write.
-- Receipt duration_ms remains the capture phase before raw storage. Neither is CPU/billing time.
ALTER TABLE captures ADD COLUMN processing_ms REAL;
