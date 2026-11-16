from speakeasy.modes import load_modes, Mode

def test_builtins_present_without_file():
    m = load_modes(None)
    assert set(m) >= {"raw", "clean", "email", "prompt-polish"}
    assert m["raw"].system_prompt is None
    assert m["clean"].model.startswith("claude-haiku")

def test_translate_builtin_present_and_not_a_rewrite_mode():
    m = load_modes(None)
    assert "translate" in m
    assert m["translate"].system_prompt is None


def test_user_yaml_overrides_and_adds(tmp_path):
    p = tmp_path / "modes.yaml"
    p.write_text(
        "clean:\n  model: claude-sonnet-5\n"
        "jira:\n  system_prompt: Rewrite as a JIRA comment.\n")
    m = load_modes(p)
    assert m["clean"].model == "claude-sonnet-5"
    assert m["jira"].system_prompt.startswith("Rewrite")
    assert m["raw"].system_prompt is None   # raw not overridable
