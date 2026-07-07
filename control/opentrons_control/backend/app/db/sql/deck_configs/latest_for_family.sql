SELECT id, version, origin_owner_name, origin_name, origin_version
FROM deck_configs
WHERE owner = :owner AND name = :name
ORDER BY version DESC
LIMIT 1;