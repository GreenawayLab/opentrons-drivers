SELECT DISTINCT ON (ap.owner, ap.name)
       ap.id, ap.name, ap.major, ap.minor, ap.patch, ap.config_id, ap.created_at, u.name AS owner_name
FROM action_plans ap
JOIN users u ON u.id = ap.owner
WHERE ap.owner <> :owner
ORDER BY ap.owner, ap.name, ap.major DESC, ap.minor DESC, ap.patch DESC;