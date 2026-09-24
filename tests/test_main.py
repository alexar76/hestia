from hestia.__main__ import main


def test_main_starts_uvicorn_factory(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HESTIA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("HESTIA_RUNTIME", "stub")
    monkeypatch.setenv("HESTIA_HOST", "127.0.0.1")
    monkeypatch.setenv("HESTIA_PORT", "9480")
    called = {}

    def fake_run(app, **kwargs):
        called["app"] = app
        called.update(kwargs)

    monkeypatch.setattr("hestia.__main__.uvicorn.run", fake_run)
    main()
    assert called["app"] == "hestia.app:build_app"
    assert called["factory"] is True
    assert called["host"] == "127.0.0.1"
    assert called["port"] == 9480
