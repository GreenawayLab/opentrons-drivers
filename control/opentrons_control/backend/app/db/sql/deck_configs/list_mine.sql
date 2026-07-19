SELECT DISTINCT ON (name)
       id, name, major, minor, patch, created_at, description,
       origin_owner_name, origin_name, origin_major, origin_minor, origin_patch
FROM deck_configs
WHERE owner = :owner
ORDER BY name, major DESC, minor DESC, patch DESC;