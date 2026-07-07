SELECT v.id, v.version, v.created_at
FROM deck_configs v
JOIN deck_configs anchor ON anchor.owner = v.owner AND anchor.name = v.name
WHERE anchor.id = :id
ORDER BY v.version DESC;