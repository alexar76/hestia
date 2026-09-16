from hestia.config import Settings
from hestia.policy import assert_safe_docker_argv, profile_from_settings
from hestia.runtime.stub import docker_spec_for_tests


def test_docker_argv_never_mounts_the_socket(tmp_path) -> None:
    settings = Settings.for_test(tmp_path)
    profile = profile_from_settings(settings)
    argv = docker_spec_for_tests(
        docker_bin="docker",
        slug="demo",
        digest="sha256:" + ("ab" * 32),
        profile=profile,
    )
    joined = " ".join(argv)
    assert_safe_docker_argv(argv)
    assert "/var/run/docker.sock" not in joined
    assert "--privileged" not in argv
    assert "--network" in argv
    assert argv[argv.index("--network") + 1] != "host"
    assert "-p" not in argv
    assert "--publish" not in argv
    assert "--cap-drop" in argv
    assert "ALL" in argv
    assert "--read-only" in argv
    assert "no-new-privileges:true" in argv
    assert "--user" in argv
    assert "65532:65532" in argv
    assert "--pids-limit" in argv
    assert "64" in argv
