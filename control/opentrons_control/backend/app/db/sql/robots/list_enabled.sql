SELECT robot_id, host, ssh_user, agent_port, key_name
FROM robots
WHERE enabled = TRUE
ORDER BY robot_id;