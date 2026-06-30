SELECT id, name, password_hash, role
FROM users
WHERE name = :name AND deleted_at IS NULL;