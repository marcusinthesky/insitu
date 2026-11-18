"""Exercise scoped adoption using real Copier updates and local Git templates."""

import importlib.util
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("template", ROOT / "scripts/template.py")
template = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(template)


def write(root, name, content):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def release(source, tag):
    template.commit(source)
    template.git(source, "tag", tag)
    return template.git(source, "rev-parse", "HEAD").decode().strip()


@pytest.fixture
def adopted(tmp_path):
    source, rendered, project = (
        tmp_path / name for name in ("source", "rendered", "project")
    )
    source.mkdir()
    config = {
        "_subdirectory": "template",
        "project_name": {"default": "Insitu"},
        "languages": {"type": "yaml", "default": []},
        "workflows": {"type": "yaml", "default": []},
        "agents": {"type": "yaml", "default": ["codex", "claude"]},
        "secrets": {"type": "bool", "default": False},
    }
    write(source, "copier.yml", yaml.safe_dump(config))
    write(
        source,
        "template/{{ _copier_conf.answers_file }}.jinja",
        "{{ _copier_answers | to_nice_yaml }}",
    )
    for name, body in {
        "infra/nix/check.sh": "#!/bin/sh\n# common header\necho first\n# common footer\n",
        "infra/nix/check-text.sh": "#!/bin/sh\necho text\n",
        ".agents/README.md": "Agent tooling\n",
        ".agents/justfile": "check:\n    echo first\n",
        ".agents/AGENTS.md": "Upstream starter instructions\n",
        "README.md": "Upstream starter readme\n",
        "LICENSE": "Upstream license\n",
        "Cargo.toml": "[package]\nname = 'starter'\n",
        "crates/insitu/src/main.rs": "fn main() {}\n",
        "python/insitu/__init__.py": "# starter SDK\n",
        "packaging/npm/package.json": '{"name":"starter"}\n',
    }.items():
        write(source, "template/" + name, body)
    template.git(source, "init", "--quiet")
    revision = release(source, "v1.0.0")
    template.copier(
        tmp_path,
        "copy",
        "--defaults",
        "--skip-tasks",
        "--quiet",
        "--vcs-ref",
        revision,
        str(source),
        str(rendered),
    )
    project.mkdir()
    shutil.copy2(ROOT / "exordio.toml", project / "exordio.toml")
    for name in template.owned_paths(project):
        if (rendered / name).is_file():
            target = project / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(rendered / name, target)
    answers = yaml.safe_load((project / template.ANSWERS).read_text())
    template.save_answers(project, {**answers, "_commit": revision})
    for name, body in {
        "README.md": "Authored Insitu documentation\n",
        "LICENSE": "MIT: authored license\n",
        "Cargo.toml": "[workspace]\nmembers = ['crates/insitu']\n",
        "crates/insitu/src/main.rs": 'fn main() { println!("Insitu"); }\n',
        "python/insitu/__init__.py": "# authored Python SDK\n",
        "packaging/npm/package.json": '{"name":"insitu-cli"}\n',
        ".agents/AGENTS.md": "Authored Insitu instructions\n",
        "infra/nix/project-owned.sh": "#!/bin/sh\necho authored\n",
    }.items():
        write(project, name, body)
    template.git(project, "init", "--quiet")
    template.commit(project)
    return source, project, revision


def authored_snapshot(project):
    owned = template.owned_paths(project)
    return {
        name: value
        for name, value in template.snapshot(project).items()
        if name not in owned
    }


def test_replay_preserves_dirty_authored_content_without_writing(adopted):
    _, project, revision = adopted
    write(project, "README.md", "A new, uncommitted authored sentence.\n")
    path = project / ".agents/justfile"
    path.write_text(path.read_text() + "\n# Local formatting and commentary.\n")
    before = template.snapshot(project)
    status = template.git(project, "status", "--porcelain")
    patch, _ = template.prepare(project, revision)
    assert patch == b""
    assert template.snapshot(project) == before
    assert template.git(project, "status", "--porcelain") == status


def test_owned_customization_survives_nonconflicting_update(adopted):
    source, project, _ = adopted
    name = "infra/nix/check.sh"
    path = project / name
    path.write_text("# Downstream policy\n" + path.read_text())
    template.commit(project)
    upstream = source / "template" / name
    upstream.write_text(upstream.read_text().replace("echo first", "echo second"))
    revision = release(source, "v1.1.0")
    template.update(project, revision)
    assert path.read_text().startswith("# Downstream policy\n")
    assert "echo second" in path.read_text()


def test_inherited_git_environment_cannot_redirect_updates(
    adopted, tmp_path, monkeypatch
):
    source, project, _ = adopted
    sentinel = tmp_path / "caller"
    sentinel.mkdir()
    write(sentinel, "authored.txt", "Protect the caller's worktree and index.\n")
    template.git(sentinel, "init", "--quiet")
    template.commit(sentinel)
    before = template.snapshot(sentinel)
    index = sentinel / ".git/index"
    original_index = index.read_bytes()
    original_head = template.git(sentinel, "rev-parse", "HEAD")
    write(source, "template/.agents/README.md", "A real upstream update\n")
    revision = release(source, "v1.1.0")
    for variable, value in {
        "GIT_DIR": sentinel / ".git",
        "GIT_COMMON_DIR": sentinel / ".git",
        "GIT_WORK_TREE": sentinel,
        "GIT_INDEX_FILE": index,
        "GIT_OBJECT_DIRECTORY": sentinel / ".git/objects",
    }.items():
        monkeypatch.setenv(variable, str(value))
    template.update(project, revision)
    assert (project / ".agents/README.md").read_text() == "A real upstream update\n"
    assert template.snapshot(sentinel) == before
    assert index.read_bytes() == original_index
    assert template.git(sentinel, "rev-parse", "HEAD") == original_head


def test_two_updates_preserve_authored_files_and_bound_additions_removals(adopted):
    source, project, _ = adopted
    # Project-owned content continues evolving independently after adoption.
    write(project, ".agents/AGENTS.md", "Revised authored Insitu instructions\n")
    write(project, "docs/new-guide.md", "A new project-owned guide\n")
    template.commit(project)
    authored = authored_snapshot(project)
    write(
        source,
        "template/infra/nix/check.sh",
        "#!/bin/sh\n# common header\necho second\n# common footer\n",
    )
    (source / "template/.agents/README.md").rename(
        source / "template/.agents/renamed.md"
    )
    (source / "template/LICENSE").unlink()
    write(source, "template/README.md", "Changed upstream README\n")
    write(
        source, "template/crates/insitu/src/main.rs", "Changed upstream application\n"
    )
    write(source, "template/infra/nix/unselected.sh", "New unowned tool\n")
    write(source, "template/unknown-root.md", "New unowned root file\n")
    second = release(source, "v1.1.0")
    before = template.snapshot(project)
    patch, _ = template.prepare(project, second)
    assert b"echo second" in patch
    assert template.snapshot(project) == before
    template.update(project, second)
    assert "echo second" in (project / "infra/nix/check.sh").read_text()
    assert not (project / ".agents/README.md").exists()
    assert not (project / ".agents/renamed.md").exists()
    assert not (project / "unknown-root.md").exists()
    assert not (project / "infra/nix/unselected.sh").exists()
    assert authored_snapshot(project) == authored
    template.commit(project)

    # Removing a whole upstream directory must retain authored children, and
    # newly introduced files are admitted only when explicitly owned already.
    shutil.rmtree(source / "template/infra")
    write(source, "template/.agents/codex.toml", 'model = "test"\n')
    write(source, "template/.agents/justfile", "check:\n    echo third\n")
    write(source, "template/python/insitu/__init__.py", "# changed upstream SDK\n")
    write(source, "template/new-directory/file.md", "Unselected new directory\n")
    third = release(source, "v1.2.0")
    template.update(project, third)
    assert not (project / "infra/nix/check.sh").exists()
    assert not (project / "infra/nix/check-text.sh").exists()
    assert (project / "infra/nix/project-owned.sh").is_file()
    assert (project / ".agents/codex.toml").read_text() == 'model = "test"\n'
    assert not (project / "new-directory").exists()
    assert authored_snapshot(project) == authored
    answers = yaml.safe_load((project / template.ANSWERS).read_text())
    assert answers["_src_path"] == str(source)
    assert answers["_commit"] == third
    assert answers["languages"] == answers["workflows"] == []


def test_rejects_migrations_even_with_user_wide_template_trust(
    adopted, tmp_path, monkeypatch
):
    source, project, _ = adopted
    marker = tmp_path / "migration-must-not-run"
    settings = tmp_path / "trusted-settings.yml"
    settings.write_text(yaml.safe_dump({"trust": [tempfile.gettempdir() + "/"]}))
    monkeypatch.setenv("COPIER_SETTINGS_PATH", str(settings))
    config = yaml.safe_load((source / "copier.yml").read_text())
    config["_migrations"] = [{"version": "2.0.0", "before": [["touch", str(marker)]]}]
    write(source, "copier.yml", yaml.safe_dump(config))
    revision = release(source, "v2.0.0")
    before = template.snapshot(project)
    with pytest.raises(subprocess.CalledProcessError) as raised:
        template.update(project, revision)
    assert b"migrations" in raised.value.stderr
    assert not marker.exists()
    assert template.snapshot(project) == before


def test_rejects_annotated_tag_object_as_commit_pin(adopted):
    source, project, _ = adopted
    template.git(source, "tag", "--annotate", "v1.0.1", "--message", "Annotated tag")
    tag_object = template.git(source, "rev-parse", "v1.0.1").decode().strip()
    with pytest.raises(ValueError, match="commit object SHA"):
        template.prepare(project, tag_object)


def test_conflict_does_not_apply_any_files(adopted):
    source, project, _ = adopted
    path = "infra/nix/check.sh"
    write(
        project,
        path,
        (project / path).read_text().replace("echo first", "echo downstream"),
    )
    template.commit(project)
    write(
        source,
        "template/" + path,
        (source / "template" / path).read_text().replace("echo first", "echo upstream"),
    )
    write(source, "template/.agents/README.md", "Another owned update\n")
    revision = release(source, "v1.1.0")
    before = template.snapshot(project)
    with pytest.raises((ValueError, subprocess.CalledProcessError)):
        template.update(project, revision)
    assert template.snapshot(project) == before
    assert template.git(project, "status", "--porcelain") == b""


def test_update_refuses_dirty_checkout(adopted):
    _, project, revision = adopted
    write(project, "README.md", "Uncommitted work\n")
    before = template.snapshot(project)
    with pytest.raises(ValueError, match="Commit or stash"):
        template.update(project, revision)
    assert template.snapshot(project) == before


@pytest.mark.parametrize("revision", ["main", "v1.1.0", "HEAD", "--help", "a" * 39])
def test_requires_explicit_commit(adopted, revision):
    _, project, _ = adopted
    with pytest.raises(ValueError, match="full 40-character"):
        template.prepare(project, revision)


@pytest.mark.parametrize(
    "path",
    [
        "../README.md",
        "/README.md",
        "infra/../README.md",
        ".git/config",
        "infra/*",
        "exordio.toml",
        "scripts/template.py",
        ".copier-answers.yml",
        "infra",
    ],
)
def test_rejects_unsafe_ownership_manifest(adopted, path):
    _, project, _ = adopted
    write(
        project,
        "exordio.toml",
        f'copier_version = "9.16.0"\nfiles = [".copier-answers.yml", "{path}"]\n',
    )
    with pytest.raises(ValueError):
        template.owned_paths(project)


def test_rejects_symlinked_parent_before_reading_target(adopted, tmp_path):
    _, project, revision = adopted
    outside = tmp_path / "outside"
    (project / "infra").rename(outside)
    (project / "infra").symlink_to(outside, target_is_directory=True)
    before = (outside / "nix/check.sh").read_bytes()
    with pytest.raises(ValueError, match="Symlink"):
        template.prepare(project, revision)
    assert (outside / "nix/check.sh").read_bytes() == before


def test_rejects_upstream_capability_change_without_writing(adopted):
    source, project, _ = adopted
    write(
        source,
        "template/{{ _copier_conf.answers_file }}.jinja",
        "{{ _copier_answers | to_nice_yaml }}\nlanguages: [python]\n",
    )
    revision = release(source, "v1.1.0")
    before = template.snapshot(project)
    with pytest.raises(ValueError, match="answers requires"):
        template.update(project, revision)
    assert template.snapshot(project) == before


def test_rejects_new_default_enabled_question_without_writing(adopted):
    source, project, _ = adopted
    config = yaml.safe_load((source / "copier.yml").read_text())
    config["new_capability"] = {"type": "bool", "default": True}
    write(source, "copier.yml", yaml.safe_dump(config))
    revision = release(source, "v1.1.0")
    before = template.snapshot(project)
    with pytest.raises(ValueError, match="answers requires"):
        template.update(project, revision)
    assert template.snapshot(project) == before
    assert template.git(project, "status", "--porcelain") == b""
