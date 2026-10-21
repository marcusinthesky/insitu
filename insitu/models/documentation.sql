-- Every Markdown guide in the repository except the README that lists them.
-- Titles come from front matter, else the first heading, else the file name.
select
  path,
  coalesce(
    title,
    case
      when body like '# %'
        then rtrim(substr(body, 3, instr(body || char(10), char(10)) - 3), char(13))
    end,
    stem
  ) as title
from documents
where path != 'README.md'
order by depth, path;
