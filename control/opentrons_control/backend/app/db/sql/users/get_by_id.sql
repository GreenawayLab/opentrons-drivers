SELECT id, name, role
FROM users
WHERE id = :user_id AND deleted_at IS NULL;