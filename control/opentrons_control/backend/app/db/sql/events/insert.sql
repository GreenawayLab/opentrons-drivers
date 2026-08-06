INSERT INTO events (
    kind, status, user_id, actor, robot_id,
    plan_id, plan_name, config_id, run_id, session_token, message, detail
)
VALUES (
    :kind, :status, :user_id, :actor, :robot_id,
    :plan_id, :plan_name, :config_id, :run_id, :session_token, :message,
    CAST(:detail AS jsonb)
)
RETURNING id, created_at;