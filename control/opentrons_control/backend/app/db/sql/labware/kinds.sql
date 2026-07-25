SELECT name,
       definition -> 'metadata' ->> 'displayCategory' AS display_category
FROM labware;