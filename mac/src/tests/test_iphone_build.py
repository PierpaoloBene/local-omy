"""omy iphone build: native app (XcodeGen) when iphone/app/project.yml exists, else Flutter. No real tools run."""

import pytest

from omy import iphone


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    derived = tmp_path / "build" / "DerivedData"
    monkeypatch.setattr(iphone, "IPHONE_APP", app_dir)
    monkeypatch.setattr(iphone, "DERIVED", derived)
    monkeypatch.setattr(iphone, "APP", tmp_path / "build" / "local-omy.app")
    monkeypatch.setattr(iphone, "iphone_bundle_id", lambda team: "com.example.local-omy")
    calls = []

    def fake_run(cmd, what, cwd=None):
        calls.append((cmd, cwd))
        if cmd[0] == "xcodebuild":
            product = "LocalOmy.app" if "-project" in cmd else "Runner.app"
            (derived / "Build" / "Products" / "Release-iphoneos" / product).mkdir(parents=True)

    monkeypatch.setattr(iphone, "run", fake_run)
    return app_dir, calls


def test_native_build_uses_xcodegen_and_the_project(sandbox):
    app_dir, calls = sandbox
    (app_dir / "project.yml").write_text("name: LocalOmy\n")
    app = iphone.build("TEAM123", "UDID-1")
    assert calls[0] == (["xcodegen", "generate"], app_dir)
    cmd, cwd = calls[1]
    assert cmd[:5] == ["xcodebuild", "-project", str(app_dir / "LocalOmy.xcodeproj"), "-scheme", "LocalOmy"]
    assert "id=UDID-1" in cmd and "DEVELOPMENT_TEAM=TEAM123" in cmd
    assert "LOCAL_OMY_BUNDLE_ID=com.example.local-omy" in cmd and "-configuration" in cmd
    assert app == iphone.APP and app.exists()


def test_flutter_build_without_project_yml(sandbox):
    _, calls = sandbox
    iphone.build("TEAM123", None)
    assert calls[0][0][:2] == ["flutter", "build"]
    cmd = calls[1][0]
    assert "-workspace" in cmd and "generic/platform=iOS" in cmd
