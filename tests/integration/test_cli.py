from chargeshare.cli import main


def test_serve_refuses_default_admin_password_without_dev(tmp_path, monkeypatch, caplog):
    monkeypatch.delenv("CHARGESHARE_ADMIN_PASSWORD", raising=False)
    assert main(["serve", "--db", str(tmp_path / "x.db")]) == 2
    assert "--dev" in caplog.text
    assert not (tmp_path / "x.db").exists()  # 拒絕時不做任何事
