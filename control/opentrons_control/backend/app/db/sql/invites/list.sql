SELECT i.code, i.target_role, i.created_at, i.used_at,
       c.name AS created_by_name, u.name AS used_by_name
FROM user_invites i
LEFT JOIN users c ON c.id = i.created_by
LEFT JOIN users u ON u.id = i.used_by
ORDER BY i.created_at DESC;