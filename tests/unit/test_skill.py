from importlib import resources

from browser_harness import run


def _frontmatter(text: str) -> str:
    assert text.startswith("---\n")
    end = text.find("\n---\n", 4)
    assert end != -1
    return text[4:end]


def test_packaged_skill_frontmatter_is_valid_simple_yaml():
    text = run._skill_text()
    metadata = {}

    for line in _frontmatter(text).splitlines():
        key, separator, value = line.partition(":")
        assert separator == ":", line
        assert key in {"name", "description"}
        assert key.strip() == key
        value = value.strip()
        assert value, key

        if value[0] in {"'", '"'}:
            assert value[-1] == value[0], line
            parsed = value[1:-1]
        else:
            parsed = value
            assert ": " not in parsed, line

        metadata[key] = parsed

    assert metadata == {
        "name": "browser-harness",
        "description": "Control a real browser via CDP: clicking, typing, navigation, logged-in sessions, JS-rendered or bot-protected pages. Not for plain HTTP fetches of public content - use curl for those.",
    }


def test_skill_text_follows_a_symlink_checked_out_as_a_text_file(tmp_path, monkeypatch):
    (tmp_path / "SKILL.md").write_text("---\nname: browser-harness\n---\n", encoding="utf-8")
    package = tmp_path / "src" / "browser_harness"
    package.mkdir(parents=True)
    (package / "SKILL.md").write_text("../../SKILL.md", encoding="utf-8")
    monkeypatch.setattr(resources, "files", lambda name: package)

    assert run._skill_text() == "---\nname: browser-harness\n---\n"
