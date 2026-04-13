"""Placeholder test to keep pytest exit code 0 on an empty project."""


def test_package_imports() -> None:
    import magpie  # noqa: F401


def test_main_entrypoint(capsys) -> None:
    from magpie.__init__ import main as magpie_main

    magpie_main()
    captured = capsys.readouterr()
    assert "Hello from magpie!" in captured.out
