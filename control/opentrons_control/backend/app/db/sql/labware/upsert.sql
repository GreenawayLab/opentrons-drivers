INSERT INTO labware (name, definition, created_by, updated_by, updated_at)
VALUES (:name, CAST(:definition AS JSONB), :actor, :actor, now())
ON CONFLICT (name) DO UPDATE
   SET definition = EXCLUDED.definition,
       updated_by = EXCLUDED.updated_by,
       updated_at = now();