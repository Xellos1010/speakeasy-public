from speakeasy.sysinfo import SystemSpecs, detect_specs, recommend_engine, ENGINE_INFO

def test_detect_specs_parses_sysctl_output():
    def fake_run(cmd):
        if cmd[-1] == "machdep.cpu.brand_string":
            return b"Apple M2 Pro\n"
        if cmd[-1] == "hw.memsize":
            return b"17179869184\n"  # 16 GiB
        return b""
    specs = detect_specs(run=fake_run)
    assert specs.chip == "Apple M2 Pro"
    assert specs.ram_gb == 16.0
    assert specs.is_apple_silicon is True

def test_detect_specs_recognizes_non_apple_silicon():
    def fake_run(cmd):
        if cmd[-1] == "machdep.cpu.brand_string":
            return b"Intel(R) Core(TM) i7-9750H\n"
        return b"8589934592"  # 8 GiB
    specs = detect_specs(run=fake_run)
    assert specs.is_apple_silicon is False

def test_detect_specs_handles_sysctl_failure_gracefully():
    def fake_run(cmd):
        raise FileNotFoundError("no sysctl")
    specs = detect_specs(run=fake_run)
    assert specs.chip == "Unknown"
    assert specs.ram_gb == 0.0

def test_recommend_engine_is_parakeet_regardless_of_hardware():
    assert recommend_engine(SystemSpecs("Intel Core i5", 8.0, False)) == "parakeet"
    assert recommend_engine(SystemSpecs("Apple M3 Max", 64.0, True)) == "parakeet"

def test_engine_info_only_lists_real_dictation_engines():
    # whisper-translate is an internal detail of translate mode, not a
    # user-facing dictation engine choice -- must never appear here.
    assert set(ENGINE_INFO) == {"parakeet", "whisper"}
    for info in ENGINE_INFO.values():
        assert "label" in info and "detail" in info
