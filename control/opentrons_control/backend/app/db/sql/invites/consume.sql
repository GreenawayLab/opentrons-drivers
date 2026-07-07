UPDATE user_invites
   SET used_by = :user_id, used_at = now()
 WHERE code = :code AND used_by IS NULL
RETURNING code;