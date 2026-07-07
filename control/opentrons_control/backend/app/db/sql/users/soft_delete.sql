UPDATE users SET deleted_at = now()
WHERE id = :user_id AND deleted_at IS NULL;