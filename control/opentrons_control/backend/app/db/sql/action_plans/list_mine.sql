SELECT DISTINCT ON (name)
       id, name, major, minor, patch, config_id, created_at, description,
       origin_owner_name, origin_name, origin_major, origin_minor, origin_patch
FROM action_plans
WHERE owner = :owner
ORDER BY name, major DESC, minor DESC, patch DESC;