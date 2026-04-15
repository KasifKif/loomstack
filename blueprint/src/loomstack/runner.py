"""Blueprint entry point — dispatched by NemoClaw via `python -m loomstack.runner`."""

import sys


def main() -> None:
    if len(sys.argv) >= 2 and sys.argv[1] == "weaver":
        _run_weaver()
        return

    print("usage: python -m loomstack.runner <command>", file=sys.stderr)
    print("commands: weaver", file=sys.stderr)
    sys.exit(1)


def _run_weaver() -> None:
    import uvicorn  # noqa: PLC0415

    from loomstack.weaver.app import create_app  # noqa: PLC0415
    from loomstack.weaver.config import get_settings  # noqa: PLC0415

    settings = get_settings()
    app = create_app()
    uvicorn.run(app, host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
