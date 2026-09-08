"""Set Alembic's database URL without configparser eating the password.

Phase 18, and it was found on a real deployment rather than in review. The pre-deploy
migration died before running anything:

    ValueError: invalid interpolation syntax in
    'postgresql+psycopg://postgres:...%25HA%40M...' at position 32

`alembic.Config` is a thin wrapper over `configparser.ConfigParser`, which uses `%` as its
interpolation escape. `config.set_main_option(...)` therefore reads a percent-encoded
password as a broken variable substitution and raises. This outlet's Supabase password
contains reserved characters, so its percent-encoded form contains `%` -- and a password is
exactly the kind of value nobody thinks of as "config syntax".

The fix is to write through to the underlying parser with `%` escaped as `%%`, which is
configparser's own convention for a literal percent. `get_main_option()` unescapes it on the
way out, so callers see the URL they passed in.

Why a shared function rather than the fix applied twice: there are two callers -- Alembic's
env.py and the test suite's fixture -- and a bug fixed in one place and forgotten in the
other is exactly the failure mode CLAUDE.md §6.9 records for `_CONSTRAINT_ERRORS`, which was
forgotten twice. One function, one place to be right.
"""

from __future__ import annotations

from alembic.config import Config


def set_alembic_url(config: Config, url: str) -> None:
    """Put `url` on `config` as `sqlalchemy.url`, safe for any password.

    Equivalent to `config.set_main_option("sqlalchemy.url", url)` except that a `%` in the
    URL is stored literally instead of being parsed as configparser interpolation.
    """
    section = config.config_ini_section

    # config.file_config is the ConfigParser underneath. Writing to it directly skips
    # Alembic's set_section_option, which is the layer that runs interpolation on the way
    # in. `%%` is configparser's escape for a literal `%`, and get_main_option() reverses
    # it -- so this is a storage detail, never something a caller has to undo.
    if not config.file_config.has_section(section):
        config.file_config.add_section(section)
    config.file_config.set(section, "sqlalchemy.url", url.replace("%", "%%"))
