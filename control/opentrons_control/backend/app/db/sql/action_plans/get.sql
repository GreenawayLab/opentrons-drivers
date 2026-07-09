SELECT ap.id, ap.owner, u.name AS owner_name, ap.name,
       ap.major, ap.minor, ap.patch, ap.config_id, ap.steps,
       ap.origin_owner_name, ap.origin_name, ap.origin_major, ap.origin_minor, ap.origin_patch,
       ap.created_at
FROM action_plans ap
JOIN users u ON u.id = ap.owner
WHERE ap.id = :id;