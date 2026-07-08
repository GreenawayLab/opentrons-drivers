SELECT id, major, minor, patch, config,
       origin_owner_name, origin_name, origin_major, origin_minor, origin_patch
FROM deck_configs
WHERE owner = :owner AND name = :name
ORDER BY major DESC, minor DESC, patch DESC
LIMIT 1;