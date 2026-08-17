"""The role hierarchy (CLAUDE.md §8).

Every one of the nine (held, minimum) pairs is asserted explicitly rather than looped,
because this table *is* the permission model. If someone changes `satisfies()`, the
failing test name should say exactly which privilege boundary moved.
"""

from __future__ import annotations

from app.core.roles import Role, satisfies


def test_attendant_satisfies_attendant() -> None:
    assert satisfies(Role.attendant, Role.attendant) is True


def test_attendant_does_not_satisfy_manager() -> None:
    assert satisfies(Role.attendant, Role.manager) is False


def test_attendant_does_not_satisfy_admin() -> None:
    assert satisfies(Role.attendant, Role.admin) is False


def test_manager_satisfies_attendant() -> None:
    """§8: a manager can do everything an attendant can."""
    assert satisfies(Role.manager, Role.attendant) is True


def test_manager_satisfies_manager() -> None:
    assert satisfies(Role.manager, Role.manager) is True


def test_manager_does_not_satisfy_admin() -> None:
    """§8: locking a shift and entering fuel prices are admin-only."""
    assert satisfies(Role.manager, Role.admin) is False


def test_admin_satisfies_attendant() -> None:
    assert satisfies(Role.admin, Role.attendant) is True


def test_admin_satisfies_manager() -> None:
    assert satisfies(Role.admin, Role.manager) is True


def test_admin_satisfies_admin() -> None:
    assert satisfies(Role.admin, Role.admin) is True


def test_role_values_match_the_database_enum() -> None:
    """The PostgreSQL type `membership_role` is created from these exact strings.

    A mismatch would not fail at import -- it would fail at the first INSERT, in
    production, which is a bad place to discover a typo.
    """
    assert {role.value for role in Role} == {"admin", "manager", "attendant"}


def test_role_compares_equal_to_its_string() -> None:
    """StrEnum, so a role read back from the database compares without conversion."""
    assert Role.manager == "manager"
