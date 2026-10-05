"""hermes-proactive-secretary plugin entry point.

``register(ctx)`` does the minimum needed to expose the plugin surface and
nothing else:

- adds ``<plugin_root>/src`` to ``sys.path`` so the bundled ``proactive_pilot``
  package imports;
- registers the bundled ``proactive-secretary`` skill;
- registers the ``hermes secretary`` CLI command.

The heavy ``proactive_pilot`` package is imported lazily by the CLI handlers, so
loading this plugin never slows down unrelated ``hermes`` startup, and no
sources, scheduler jobs, or network calls are touched at registration time.
"""
from __future__ import annotations

import sys
from pathlib import Path


def _plugin_root() -> Path:
    return Path(__file__).resolve().parent


def _setup_secretary_cli(subparser) -> None:
    from .secretary_cli import register_cli
    register_cli(subparser)


def _handle_secretary_cli(args):
    from .secretary_cli import secretary_command
    return secretary_command(args)


def register(ctx) -> None:
    """Wire the plugin into Hermes. Called exactly once at load time."""
    root = _plugin_root()
    src = root / "src"
    if src.is_dir():
        src_str = str(src)
        if src_str not in sys.path:
            sys.path.insert(0, src_str)

    # Keep the host-built context for the registered handler closure so the
    # ordinary pipeline can use the supported ctx.llm lane when invoked from
    # the plugin CLI. This is the context Hermes created and passed in; the
    # plugin never constructs one.
    from . import secretary_cli as _cli
    _cli.set_native_context(ctx)

    skill = root / "skills" / "proactive-secretary" / "SKILL.md"
    if skill.is_file():
        ctx.register_skill("proactive-secretary", skill,
                           description="Proactive secretary setup, operation and limits.")

    ctx.register_cli_command(
        name="secretary",
        help="Proactive secretary: one-command setup, monitoring and reminders",
        setup_fn=_setup_secretary_cli,
        handler_fn=_handle_secretary_cli,
        description=("Proactive secretary over your own authorised mail, calendar and "
                     "tasks plus public sources, with native scheduled jobs."),
    )
