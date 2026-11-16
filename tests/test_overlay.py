from speakeasy.overlay import OverlayModel, level_to_bar_heights

def test_labels():
    assert OverlayModel().label() == ""
    assert OverlayModel(state="recording", mode_name="email").label() == "● Recording — email"
    assert OverlayModel(state="transcribing").label() == "… Transcribing"
    assert OverlayModel(state="rewriting", mode_name="clean").label() == "✍ Rewriting — clean"

def test_level_to_bar_heights_silence_hits_visibility_floor():
    heights = level_to_bar_heights(0.0)
    assert len(heights) == 5
    assert all(h == heights[0] for h in heights)
    assert heights[0] > 0

def test_level_to_bar_heights_has_waveform_silhouette():
    heights = level_to_bar_heights(0.3)
    assert heights == heights[::-1]          # symmetric
    assert heights[2] > heights[0]            # center bar tallest
    assert heights[0] > 0.15                  # above the silent floor

def test_level_to_bar_heights_clamps_loud_input():
    heights = level_to_bar_heights(5.0)
    assert max(heights) <= 1.0

def test_done_label_maps_known_status_codes():
    assert OverlayModel(state="done", message="injected").label() == "✓ Inserted"
    assert OverlayModel(state="done", message="clipboard_only").label() == \
        "📋 Copied — paste manually (secure field)"
    assert OverlayModel(state="done", message="rewrite_failed").label() == \
        "⚠ Rewrite unavailable — raw text inserted"

def test_done_label_falls_back_to_raw_code_for_unknown_status():
    assert OverlayModel(state="done", message="something_new").label() == "something_new"

def test_done_label_falls_back_to_done_when_no_status():
    assert OverlayModel(state="done").label() == "Done"
