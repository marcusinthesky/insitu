select count(*) as total from {{ ref("all_tasks") }}
