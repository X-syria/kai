"""The Tool Registry: application-controlled, sealed, deterministic.

The registry's whole job is to make *tool choice* a static fact, not a runtime
decision. These tests pin that behavior down: names are validated, duplicates
are a programming error, unknown/variated names are refused, and the registry
cannot be mutated once built.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from kai.tools.catalog import default_registry
from kai.tools.definition import ToolDefinition
from kai.tools.errors import (
    DuplicateToolError,
    ToolNotFoundError,
    ToolRegistryLockedError,
)
from kai.tools.execution import VerificationContract
from kai.tools.registry import ToolRegistry
from kai.tools.risk import RiskLevel

EXPECTED_MVP_TOOLS = {
    "contacts.match",
    "calendar.read_slots",
    "calendar.get_event",
    "calendar.create_event",
    "gmail.search",
    "gmail.read",
    "gmail.send",
    "task.prepare",
}


class _NoArgs(BaseModel):
    pass


def _definition(name: str) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description=f"tool {name} for registry tests",
        input_schema=_NoArgs,
        risk_level=RiskLevel.LOW,
        capability="test",
        executor=object(),  # type: ignore[arg-type]
        verification_contract=VerificationContract.MANUAL,
    )


class TestDefaultCatalog:
    def test_default_registry_is_sealed_with_exactly_the_mvp_tools(self) -> None:
        registry = default_registry()
        assert set(registry.names()) == EXPECTED_MVP_TOOLS
        assert registry.is_locked

    def test_default_tools_carry_the_declared_design(self) -> None:
        registry = default_registry()
        calendar_create = registry.get("calendar.create_event")
        assert calendar_create.risk_level is RiskLevel.MEDIUM
        assert (
            calendar_create.verification_contract
            is VerificationContract.READ_AFTER_CREATE
        )
        gmail_send = registry.get("gmail.send")
        assert gmail_send.risk_level is RiskLevel.MEDIUM
        assert gmail_send.verification_contract is VerificationContract.READ_AFTER_SEND
        assert registry.get("contacts.match").risk_level is RiskLevel.LOW
        prepare = registry.get("task.prepare")
        assert prepare.risk_level is RiskLevel.LOW
        assert prepare.verification_contract is VerificationContract.READ_BACKED
        assert prepare.requires_account is False

    def test_sealed_registry_refuses_registration(self) -> None:
        registry = default_registry()
        with pytest.raises(ToolRegistryLockedError):
            registry.register(_definition("rogue.tool"))


class TestRegistration:
    def test_register_then_lock_is_deterministic(self) -> None:
        registry = ToolRegistry()
        registry.register(_definition("alpha.read"))
        registry.register(_definition("alpha.write"))
        registry.lock()
        assert set(registry.names()) == {"alpha.write", "alpha.read"}
        assert registry.contains("alpha.read")
        assert len(registry) == 2
        with pytest.raises(ToolRegistryLockedError):
            registry.register(_definition("beta.read"))

    def test_duplicate_name_is_a_programming_error(self) -> None:
        registry = ToolRegistry()
        registry.register(_definition("alpha.read"))
        with pytest.raises(DuplicateToolError):
            registry.register(_definition("alpha.read"))

    def test_build_seals_the_registry_in_one_step(self) -> None:
        registry = ToolRegistry.build([_definition("alpha.read")])
        assert registry.is_locked
        with pytest.raises(ToolRegistryLockedError):
            registry.register(_definition("beta.read"))

    @pytest.mark.parametrize(
        "name",
        [
            "tool with space",
            "gmail.send; DROP TABLE",
            ".hidden",
            "-weird",
            "UPPER.tool",
            "Gmail.Send",
        ],
    )
    def test_hostile_names_never_register(self, name: str) -> None:
        with pytest.raises(ValueError):
            ToolRegistry().register(_definition(name))

    def test_dot_names_are_valid_syntax_but_never_resolve(self) -> None:
        # Namespaced names (gmail.send) use dots by design; arbitrary-looking
        # dotted names are syntactically fine but must never resolve, because
        # they were never application-registered.
        for name in ("up.tool", "os.system"):
            registry = ToolRegistry.build([_definition(name)])
            assert registry.get_if_present(name) is not None
        registry = default_registry()
        assert registry.get_if_present("up.tool") is None
        assert registry.get_if_present("os.system") is None


class TestLookupSecurity:
    def test_unknown_tool_raises_tool_not_found(self) -> None:
        registry = default_registry()
        with pytest.raises(ToolNotFoundError):
            registry.get("not.a.tool")

    def test_variated_name_never_resolves_to_an_existing_tool(self) -> None:
        registry = default_registry()
        # A suffix is not a sibling: "gmail.sendX" must not resolve to gmail.send.
        with pytest.raises(ToolNotFoundError):
            registry.get("gmail.sendX")
        with pytest.raises(ToolNotFoundError):
            registry.get("gmail.send/")

    def test_get_if_present_returns_none_for_unknown(self) -> None:
        registry = default_registry()
        assert registry.get_if_present("gmail.send") is not None
        assert registry.get_if_present("gmail.sendX") is None

    def test_definitions_view_is_read_only(self) -> None:
        registry = default_registry()
        view = registry.definitions()
        with pytest.raises(TypeError):
            view["rogue.tool"] = _definition("rogue.tool")  # type: ignore[index]