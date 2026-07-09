DELETE FROM action_plans a
USING action_plans anchor
WHERE anchor.id = :id AND a.owner = anchor.owner AND a.name = anchor.name;