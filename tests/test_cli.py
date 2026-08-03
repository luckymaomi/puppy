from xhs_robot.cli import main
from xhs_robot.web import server


def test_no_arguments_start_the_local_console(monkeypatch) -> None:
    monkeypatch.setattr(server, "run_console", lambda: 17)

    assert main([]) == 17
