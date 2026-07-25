SELECT name,
       created_at,
       definition -> 'metadata' ->> 'displayCategory' AS display_category,
       (SELECT count(*) FROM jsonb_object_keys(definition -> 'wells')) AS well_count
FROM labware
ORDER BY name;