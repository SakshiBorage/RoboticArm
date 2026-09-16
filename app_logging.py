"""
Central logging — writes full detail, including tracebacks, to service.log so
failures are still inspectable after the terminal output has scrolled past.
Console only surfaces WARNING and above, since demo.py already narrates the
happy path with its own print() statements.
"""
import logging
import sys

_configured = False


def _configure():
    global _configured
    if _configured:
        return

    root = logging.getLogger("factory_arm")
    root.setLevel(logging.DEBUG)

    file_handler = logging.FileHandler("service.log")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s"))
    root.addHandler(file_handler)

    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setLevel(logging.WARNING)
    console_handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    root.addHandler(console_handler)

    _configured = True


def get_logger(name):
    _configure()
    return logging.getLogger(f"factory_arm.{name}")
