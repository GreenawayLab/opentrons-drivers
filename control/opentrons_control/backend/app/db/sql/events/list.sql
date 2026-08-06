SELECT id, created_at, kind, status, actor, robot_id,
       plan_name, config_id, run_id, session_token, message
FROM events
WHERE (CAST(:robot_id AS text)   IS NULL OR robot_id = CAST(:robot_id AS text))
  AND (CAST(:user_id  AS bigint) IS NULL OR user_id  = CAST(:user_id  AS bigint))
  AND (CAST(:kind     AS text)   IS NULL OR kind     = CAST(:kind     AS text))
ORDER BY created_at DESC
LIMIT CAST(:limit AS integer);