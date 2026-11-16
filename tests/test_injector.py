from speakeasy.injector import inject, replace_last

class FakePasteboard:
    def __init__(self, initial="old"): self.value = initial; self.sets = []
    def get(self): return self.value
    def set(self, s): self.value = s; self.sets.append(s)

def test_paste_and_restore():
    pb, events = FakePasteboard("old"), []
    status = inject("hello", pasteboard=pb, post_paste=lambda: events.append("v"),
                    secure_check=lambda: False, sleep=lambda s: None)
    assert status == "injected"
    assert pb.sets == ["hello", "old"]      # set text, then restored
    assert events == ["v"]

def test_secure_input_clipboard_only():
    pb, events = FakePasteboard("old"), []
    status = inject("pw", pasteboard=pb, post_paste=lambda: events.append("v"),
                    secure_check=lambda: True, sleep=lambda s: None)
    assert status == "clipboard_only"
    assert pb.value == "pw" and events == []

def test_restore_even_if_paste_raises():
    pb = FakePasteboard("old")
    def boom(): raise RuntimeError("nope")
    try:
        inject("x", pasteboard=pb, post_paste=boom,
               secure_check=lambda: False, sleep=lambda s: None)
    except RuntimeError:
        pass
    assert pb.value == "old"


# --- replace_last: in-place translate-mode correction (spec item 5) --------

def test_replace_last_backspaces_old_len_then_pastes_new_when_focus_unchanged():
    pb, backspaces, pastes = FakePasteboard("clip"), [], []
    status = replace_last(
        "hola mundo", "hello world", expected_app="TextEdit",
        frontmost=lambda: "TextEdit",
        pasteboard=pb, post_paste=lambda: pastes.append("v"),
        post_backspace=lambda: backspaces.append("bs"),
        secure_check=lambda: False, sleep=lambda s: None)
    assert status == "replaced"
    assert len(backspaces) == len("hola mundo")
    assert pastes == ["v"]
    assert pb.sets == ["hello world", "clip"]     # set new text, then restored


def test_replace_last_aborts_when_frontmost_app_changed():
    pb, backspaces, pastes = FakePasteboard("clip"), [], []
    status = replace_last(
        "hola mundo", "hello world", expected_app="TextEdit",
        frontmost=lambda: "Safari",             # user switched apps
        pasteboard=pb, post_paste=lambda: pastes.append("v"),
        post_backspace=lambda: backspaces.append("bs"),
        secure_check=lambda: False, sleep=lambda s: None)
    assert status == "skipped"
    assert backspaces == [] and pastes == []
    assert pb.sets == []                          # pasteboard never touched


def test_replace_last_skips_under_secure_input():
    pb, backspaces, pastes = FakePasteboard("clip"), [], []
    status = replace_last(
        "hola mundo", "hello world", expected_app="TextEdit",
        frontmost=lambda: "TextEdit",
        pasteboard=pb, post_paste=lambda: pastes.append("v"),
        post_backspace=lambda: backspaces.append("bs"),
        secure_check=lambda: True, sleep=lambda s: None)
    assert status == "skipped"
    assert backspaces == [] and pastes == []
