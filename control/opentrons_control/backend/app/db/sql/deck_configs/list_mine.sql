SELECT DISTINCT ON (name)
       id, name, version, created_at,
       origin_owner_name, origin_name, origin_version
FROM deck_configs
WHERE owner = :owner
ORDER BY name, version DESC;