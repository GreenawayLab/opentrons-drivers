SELECT id, created_at, kind, status, message, detail
FROM events
WHERE run_id = :run_id
ORDER BY created_at ASC;