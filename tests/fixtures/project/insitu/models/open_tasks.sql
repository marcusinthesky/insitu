select * from {{ ref("all_tasks") }} where status != 'done' order by title
