import tarfile
from pathlib import Path
from speakeasy.engine.download import ensure_model, MODELS

def make_tarball(tmp_path, dirname):
    src = tmp_path / dirname
    src.mkdir()
    (src / "tokens.txt").write_text("a 1\n")
    tb = tmp_path / f"{dirname}.tar.bz2"
    with tarfile.open(tb, "w:bz2") as t:
        t.add(src, arcname=dirname)
    return tb

def test_ensure_model_extracts_and_is_idempotent(tmp_path):
    dirname = MODELS["parakeet"]["dirname"]
    tb = make_tarball(tmp_path, dirname)
    fetched = []
    def fake_fetch(url, dest):
        fetched.append(url)
        Path(dest).write_bytes(tb.read_bytes())
    models = tmp_path / "models"
    d1 = ensure_model("parakeet", models, fetch=fake_fetch)
    d2 = ensure_model("parakeet", models, fetch=fake_fetch)
    assert d1 == d2 == models / dirname
    assert (d1 / "tokens.txt").exists()
    assert len(fetched) == 1
