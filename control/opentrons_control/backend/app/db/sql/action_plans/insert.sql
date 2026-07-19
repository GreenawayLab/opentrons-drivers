INSERT INTO action_plans
       (owner, name, major, minor, patch, config_id, steps, description,
        origin_owner_name, origin_name, origin_major, origin_minor, origin_patch)
VALUES (:owner, :name, :major, :minor, :patch, :config_id, CAST(:steps AS JSONB), :description,
        :origin_owner_name, :origin_name, :origin_major, :origin_minor, :origin_patch)
RETURNING id;