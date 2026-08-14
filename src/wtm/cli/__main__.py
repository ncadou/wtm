"""Allow ``python -m wtm.cli`` to run the app (mirrors the ``wtm`` script)."""

from wtm.cli.app import main

if __name__ == "__main__":
    main()
