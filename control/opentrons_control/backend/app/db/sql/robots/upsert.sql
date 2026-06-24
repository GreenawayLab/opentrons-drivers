INSERT INTO robots (robot_id, host, ssh_user, agent_port, key_name, updated_at)
VALUES (:robot_id, :host, :ssh_user, :agent_port, :key_name, now())
ON CONFLICT (robot_id) DO UPDATE
SET host       = EXCLUDED.host,
    ssh_user   = EXCLUDED.ssh_user,
    agent_port = EXCLUDED.agent_port,
    key_name   = COALESCE(EXCLUDED.key_name, robots.key_name),
    updated_at = now();