SELECT dc.id, dc.owner, u.name AS owner_name, dc.name,
       dc.major, dc.minor, dc.patch, dc.config, dc.description,
       dc.origin_owner_name, dc.origin_name, dc.origin_major, dc.origin_minor, dc.origin_patch,
       dc.created_at
FROM deck_configs dc
JOIN users u ON u.id = dc.owner
WHERE dc.id = :id;