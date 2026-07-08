INSERT INTO deck_configs
       (owner, name, major, minor, patch, config,
        origin_owner_name, origin_name, origin_major, origin_minor, origin_patch)
VALUES (:owner, :name, :major, :minor, :patch, CAST(:config AS JSONB),
        :origin_owner_name, :origin_name, :origin_major, :origin_minor, :origin_patch)
RETURNING id;