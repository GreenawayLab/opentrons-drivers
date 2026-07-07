SELECT id, name, role, created_at, deleted_at
FROM users
ORDER BY (deleted_at IS NOT NULL), name;