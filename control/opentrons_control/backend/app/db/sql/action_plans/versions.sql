SELECT v.id, v.major, v.minor, v.patch, v.description, v.created_at
FROM action_plans v
JOIN action_plans anchor ON anchor.owner = v.owner AND anchor.name = v.name
WHERE anchor.id = :id
ORDER BY v.major DESC, v.minor DESC, v.patch DESC;