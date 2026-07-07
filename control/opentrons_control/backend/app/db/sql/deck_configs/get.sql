SELECT dc.id, dc.owner, u.name AS owner_name, dc.name, dc.version, dc.config,
       dc.origin_owner_name, dc.origin_name, dc.origin_version, dc.created_at
FROM deck_configs dc
JOIN users u ON u.id = dc.owner
WHERE dc.id = :id;