SELECT DISTINCT ON (dc.owner, dc.name)
       dc.id, dc.name, dc.version, dc.created_at, u.name AS owner_name
FROM deck_configs dc
JOIN users u ON u.id = dc.owner
WHERE dc.owner <> :owner
ORDER BY dc.owner, dc.name, dc.version DESC;