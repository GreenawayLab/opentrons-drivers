SELECT id, name, role, deleted_at
FROM users
WHERE id = :user_id;