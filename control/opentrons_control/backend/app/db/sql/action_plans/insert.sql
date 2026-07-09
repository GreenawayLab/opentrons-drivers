INSERT INTO action_plans
       (owner, name, major, minor, patch, config_id, steps,
        origin_owner_name, origin_name, origin_major, origin_minor, origin_patch)
VALUES (:owner, :name, :major, :minor, :patch, :config_id, CAST(:steps AS JSONB),
        :origin_owner_name, :origin_name, :origin_major, :origin_minor, :origin_patch)
RETURNING id;