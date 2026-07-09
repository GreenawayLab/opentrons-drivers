INSERT INTO methods (name, params, created_by)
VALUES (:name, CAST(:params AS JSONB), :created_by)
ON CONFLICT (name) DO UPDATE SET params = EXCLUDED.params;