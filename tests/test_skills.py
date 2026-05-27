"""Tests for codex_imagen._skills.

Filesystem isolation via pytest's tmp_path fixture. We never write under
the real home dir or the real cwd; discovery tests use ``extra_dirs=`` or
monkeypatch ``Path.home`` / ``Path.cwd``.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from codex_imagen._skills import (
    LoadedSkill,
    SkillBundle,
    discover_skills,
    hash_body,
    load_skill,
    load_skills,
)


# ---------------------------------------------------------------------------
# load_skill
# ---------------------------------------------------------------------------


def test_load_skill_plain_markdown_no_frontmatter(tmp_path: Path) -> None:
    p = tmp_path / "plain.md"
    p.write_text("# Hello\n\nJust a body.\n", encoding="utf-8")

    skill = load_skill(p)

    assert isinstance(skill, LoadedSkill)
    assert skill.frontmatter == {}
    assert skill.body == "# Hello\n\nJust a body.\n"
    assert skill.name == "plain"  # falls back to stem
    assert skill.description is None


def test_load_skill_with_frontmatter_extracts_fields(tmp_path: Path) -> None:
    p = tmp_path / "branded.md"
    p.write_text(
        "---\nname: brandkit\ndescription: Premium brand boards.\n---\n"
        "Body line 1.\n",
        encoding="utf-8",
    )

    skill = load_skill(p)

    assert skill.name == "brandkit"
    assert skill.description == "Premium brand boards."
    assert skill.body == "Body line 1.\n"
    assert skill.frontmatter == {
        "name": "brandkit",
        "description": "Premium brand boards.",
    }


def test_load_skill_frontmatter_strips_quotes(tmp_path: Path) -> None:
    p = tmp_path / "q.md"
    p.write_text(
        "---\n"
        'description: "with double quotes"\n'
        "title: 'with single quotes'\n"
        "---\n"
        "body\n",
        encoding="utf-8",
    )

    skill = load_skill(p)

    assert skill.frontmatter["description"] == "with double quotes"
    assert skill.frontmatter["title"] == "with single quotes"


def test_load_skill_frontmatter_ignores_comment_lines(tmp_path: Path) -> None:
    p = tmp_path / "c.md"
    p.write_text(
        "---\n"
        "# this is a comment\n"
        "name: foo\n"
        "# another comment\n"
        "---\n"
        "body\n",
        encoding="utf-8",
    )

    skill = load_skill(p)

    assert skill.frontmatter == {"name": "foo"}
    assert skill.name == "foo"


def test_load_skill_no_closing_delimiter_treats_as_body(tmp_path: Path) -> None:
    p = tmp_path / "broken.md"
    text = "---\nname: never-closed\nstill body\n"
    p.write_text(text, encoding="utf-8")

    skill = load_skill(p)

    # No closing ---, so the WHOLE file is body, frontmatter empty.
    assert skill.frontmatter == {}
    assert "name: never-closed" in skill.body
    assert skill.body.startswith("---\n")


def test_load_skill_strips_leading_blank_lines_from_body(tmp_path: Path) -> None:
    p = tmp_path / "blanks.md"
    p.write_text(
        "---\nname: x\n---\n\n\n\nReal content.\n",
        encoding="utf-8",
    )

    skill = load_skill(p)

    assert skill.body == "Real content.\n"


def test_load_skill_uses_file_stem_when_no_name_in_frontmatter(
    tmp_path: Path,
) -> None:
    p = tmp_path / "my-skill.md"
    p.write_text("---\ndescription: only desc\n---\nbody\n", encoding="utf-8")

    skill = load_skill(p)

    assert skill.name == "my-skill"
    assert skill.description == "only desc"


def test_load_skill_expands_tilde_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Point HOME / USERPROFILE at tmp_path so ~ expands there.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))

    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    (skills_dir / "t.md").write_text("hello\n", encoding="utf-8")

    skill = load_skill("~/skills/t.md")

    assert skill.body == "hello\n"
    assert skill.path == (tmp_path / "skills" / "t.md").resolve()


def test_load_skill_raises_filenotfound_on_missing(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError) as excinfo:
        load_skill(tmp_path / "does-not-exist.md")
    assert "does-not-exist.md" in str(excinfo.value)


def test_load_skill_computes_sha256_of_body(tmp_path: Path) -> None:
    p = tmp_path / "h.md"
    p.write_text("---\nname: x\n---\nthe body\n", encoding="utf-8")

    skill = load_skill(p)

    expected = hashlib.sha256(b"the body\n").hexdigest()
    assert skill.sha256 == expected


# ---------------------------------------------------------------------------
# load_skills
# ---------------------------------------------------------------------------


def test_load_skills_concatenates_bodies_with_separator(tmp_path: Path) -> None:
    a = tmp_path / "a.md"
    b = tmp_path / "b.md"
    a.write_text("AAA\n", encoding="utf-8")
    b.write_text("BBB\n", encoding="utf-8")

    bundle = load_skills([a, b])

    assert bundle.combined_body == "AAA\n\n\n---\n\nBBB\n"
    assert len(bundle.skills) == 2
    assert bundle.warnings == ()


def test_load_skills_combined_hash_changes_with_body_changes(
    tmp_path: Path,
) -> None:
    a = tmp_path / "a.md"
    a.write_text("first\n", encoding="utf-8")
    h1 = load_skills([a]).combined_hash

    a.write_text("second\n", encoding="utf-8")
    h2 = load_skills([a]).combined_hash

    assert h1 != h2


def test_load_skills_combined_hash_stable_for_same_input(
    tmp_path: Path,
) -> None:
    a = tmp_path / "a.md"
    a.write_text("stable\n", encoding="utf-8")

    assert load_skills([a]).combined_hash == load_skills([a]).combined_hash


def test_load_skills_missing_file_non_strict_warns_and_skips(
    tmp_path: Path,
) -> None:
    real = tmp_path / "real.md"
    real.write_text("real body\n", encoding="utf-8")
    missing = tmp_path / "missing.md"

    bundle = load_skills([real, missing])

    assert len(bundle.skills) == 1
    assert bundle.skills[0].body == "real body\n"
    assert len(bundle.warnings) == 1
    assert "missing.md" in bundle.warnings[0]


def test_load_skills_missing_file_strict_raises(tmp_path: Path) -> None:
    missing = tmp_path / "nope.md"
    with pytest.raises(FileNotFoundError):
        load_skills([missing], strict=True)


def test_load_skills_empty_paths_returns_empty_bundle() -> None:
    bundle = load_skills([])

    assert bundle.skills == ()
    assert bundle.combined_body == ""
    assert bundle.combined_hash == hashlib.sha256(b"").hexdigest()
    assert bundle.warnings == ()


# ---------------------------------------------------------------------------
# discover_skills
# ---------------------------------------------------------------------------


def test_discover_skills_finds_flat_md_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Isolate from real cwd / home by pointing everything at tmp_path.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "fake-home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "fake-home"))

    extra = tmp_path / "extra"
    extra.mkdir()
    (extra / "foo.md").write_text("foo body\n", encoding="utf-8")
    (extra / "bar.md").write_text("bar body\n", encoding="utf-8")

    found = discover_skills(extra_dirs=[extra])

    names = sorted(p.name for p in found)
    assert "foo.md" in names
    assert "bar.md" in names


def test_discover_skills_finds_nested_skill_md(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "fake-home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "fake-home"))

    extra = tmp_path / "extra"
    nested = extra / "product-photography"
    nested.mkdir(parents=True)
    (nested / "SKILL.md").write_text("nested body\n", encoding="utf-8")
    # A reference file that should NOT be discovered:
    (nested / "references.md").write_text("ignored\n", encoding="utf-8")

    found = discover_skills(extra_dirs=[extra])

    found_names = [str(p) for p in found]
    assert any(name.endswith("SKILL.md") for name in found_names)
    # References.md in the nested dir must NOT show up.
    assert not any(name.endswith("references.md") for name in found_names)


def test_discover_skills_silently_skips_missing_dirs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Empty tmp_path: no ./skills, no .forge/skills, no home dirs, no extras.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "absolutely-nothing"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "absolutely-nothing"))

    # Should NOT raise even though every candidate dir is missing.
    found = discover_skills()

    assert found == []


def test_discover_skills_with_extra_dirs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "fake-home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "fake-home"))

    extra1 = tmp_path / "e1"
    extra2 = tmp_path / "e2"
    extra1.mkdir()
    extra2.mkdir()
    (extra1 / "a.md").write_text("a\n", encoding="utf-8")
    (extra2 / "b.md").write_text("b\n", encoding="utf-8")

    found = discover_skills(extra_dirs=[extra1, extra2])

    found_names = sorted(p.name for p in found)
    assert found_names == ["a.md", "b.md"]


# ---------------------------------------------------------------------------
# hash_body
# ---------------------------------------------------------------------------


def test_hash_body_deterministic() -> None:
    assert hash_body("hello") == hash_body("hello")
    assert hash_body("") == hashlib.sha256(b"").hexdigest()
    assert hash_body("a") != hash_body("b")


# ---------------------------------------------------------------------------
# A couple of extra edge cases beyond the spec list (cheap insurance)
# ---------------------------------------------------------------------------


def test_load_skill_is_a_directory_raises(tmp_path: Path) -> None:
    d = tmp_path / "imadir"
    d.mkdir()
    with pytest.raises(IsADirectoryError):
        load_skill(d)


def test_load_skill_frontmatter_skips_invalid_keys(tmp_path: Path) -> None:
    p = tmp_path / "weird.md"
    p.write_text(
        "---\n"
        "name: ok\n"
        "has space: nope\n"
        "good_key: yes\n"
        "no-colon-here\n"
        "---\n"
        "body\n",
        encoding="utf-8",
    )

    skill = load_skill(p)

    assert skill.frontmatter == {"name": "ok", "good_key": "yes"}


def test_skill_bundle_dataclass_is_frozen() -> None:
    bundle = SkillBundle(
        skills=(),
        combined_body="",
        combined_hash=hash_body(""),
        warnings=(),
    )
    with pytest.raises((AttributeError, Exception)):
        bundle.combined_body = "mutated"  # type: ignore[misc]
