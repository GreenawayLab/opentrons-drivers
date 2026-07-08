INSERT INTO drafts (user_id, kind, content, updated_at)
VALUES (:user_id, :kind, CAST(:content AS JSONB), now())
ON CONFLICT (user_id, kind)
DO UPDATE SET content = EXCLUDED.content, updated_at = now();