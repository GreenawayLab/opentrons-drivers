INSERT INTO deck_configs
       (owner, name, version, config, origin_owner_name, origin_name, origin_version)
VALUES (:owner, :name, :version, CAST(:config AS JSONB),
        :origin_owner_name, :origin_name, :origin_version)
RETURNING id;