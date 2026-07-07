UPDATE users SET password_hash = :password_hash
WHERE id = :user_id AND deleted_at IS NULL;