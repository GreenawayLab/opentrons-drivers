SELECT id, created_at, kind, status, actor, robot_id,
       plan_name, config_id, run_id, session_token, message
FROM events
WHERE (:robot_id IS NULL OR robot_id = :robot_id)
  AND (:user_id  IS NULL OR user_id  = :user_id)
  AND (:kind     IS NULL OR kind     = :kind)
ORDER BY created_at DESC
LIMIT :limit;