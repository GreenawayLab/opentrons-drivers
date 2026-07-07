SELECT code, target_role
FROM user_invites
WHERE code = :code
  AND used_by IS NULL
  AND (expires_at IS NULL OR expires_at > now());