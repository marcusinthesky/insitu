-- Each workspace crate, described by the front matter of its own README.
select
  path,
  parent,
  substr(parent, length('crates/') + 1) as title,
  description,
  start_here
from documents
where name = 'README.md'
  and parent like 'crates/%'
  and parent not like 'crates/%/%'
order by parent;
