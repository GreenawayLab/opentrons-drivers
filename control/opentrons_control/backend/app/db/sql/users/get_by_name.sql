SELECT id, name, role, password_hash
FROM users
WHERE name = :name AND deleted_at IS NULL;