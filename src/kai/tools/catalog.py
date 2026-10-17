"""The application-controlled MVP tool catalog.

This is the *single* place the app declares which tools exist. Nothing at
runtime can add to it: ``default_registry()`` seals the registry at build
time, and the gateway can only resolve tool names that are here.
"""

from __future__ import annotations

from kai.tools.definition import ToolDefinition
from kai.tools.execution import ToolExecutor, VerificationContract
from kai.tools.registry import ToolRegistry
from kai.tools.risk import RiskLevel
from kai.tools.schemas import (
    CalendarCreateEventArgs,
    CalendarGetEventArgs,
    CalendarReadSlotsArgs,
    ContactsMatchArgs,
    GmailReadArgs,
    GmailSearchArgs,
    GmailSendArgs,
    PrepareTaskArgs,
)


def default_definitions(
    *,
    contacts: ToolExecutor,
    calendar: ToolExecutor,
    gmail: ToolExecutor,
    preparation: ToolExecutor | None = None,
) -> list[ToolDefinition]:
    """The MVP catalog as a plain list, ready for any registry composition."""
    defs = [
        ToolDefinition(
            name="contacts.match",
            description=(
                "Find contacts whose name, email, or phone number matches a "
                "free-text query; returns zero or more matches."
            ),
            input_schema=ContactsMatchArgs,
            risk_level=RiskLevel.LOW,
            capability="contacts",
            executor=contacts,
            verification_contract=VerificationContract.READ_BACKED,
        ),
        ToolDefinition(
            name="calendar.read_slots",
            description=(
                "Compute free and busy candidate time slots for one day in the "
                "user's calendar, within an explicit working window."
            ),
            input_schema=CalendarReadSlotsArgs,
            risk_level=RiskLevel.LOW,
            capability="calendar",
            executor=calendar,
            verification_contract=VerificationContract.READ_BACKED,
        ),
        ToolDefinition(
            name="calendar.get_event",
            description="Read one calendar event by its provider id.",
            input_schema=CalendarGetEventArgs,
            risk_level=RiskLevel.LOW,
            capability="calendar",
            executor=calendar,
            verification_contract=VerificationContract.READ_BACKED,
        ),
        ToolDefinition(
            name="calendar.create_event",
            description=(
                "Create a calendar event and verify, via read-after-create, "
                "that the event is exactly what the provider stored."
            ),
            input_schema=CalendarCreateEventArgs,
            risk_level=RiskLevel.MEDIUM,
            capability="calendar",
            executor=calendar,
            verification_contract=VerificationContract.READ_AFTER_CREATE,
        ),
        ToolDefinition(
            name="gmail.search",
            description="Search the user's mailbox and return message references.",
            input_schema=GmailSearchArgs,
            risk_level=RiskLevel.LOW,
            capability="gmail",
            executor=gmail,
            verification_contract=VerificationContract.READ_BACKED,
        ),
        ToolDefinition(
            name="gmail.read",
            description="Read one message and return its body as data.",
            input_schema=GmailReadArgs,
            risk_level=RiskLevel.LOW,
            capability="gmail",
            executor=gmail,
            verification_contract=VerificationContract.READ_BACKED,
        ),
        ToolDefinition(
            name="gmail.send",
            description=(
                "Send an email from the user's account and verify, via "
                "read-after-send, that the provider accepted the message."
            ),
            input_schema=GmailSendArgs,
            risk_level=RiskLevel.MEDIUM,
            capability="gmail",
            executor=gmail,
            verification_contract=VerificationContract.READ_AFTER_SEND,
        ),
    ]
    if preparation is not None:
        defs.append(
            ToolDefinition(
                name="task.prepare",
                description=(
                    "Prepare a local, side-effect-free task checklist and list "
                    "missing information. Does not contact external services."
                ),
                input_schema=PrepareTaskArgs,
                risk_level=RiskLevel.LOW,
                capability="task",
                executor=preparation,
                verification_contract=VerificationContract.READ_BACKED,
                requires_account=False,
            )
        )
    return defs


def default_registry() -> ToolRegistry:
    """Build the sealed, application-controlled registry of MVP tools."""
    from kai.tools.adapters.calendar import CalendarAdapter
    from kai.tools.adapters.contacts import ContactsAdapter
    from kai.tools.adapters.gmail import GmailAdapter
    from kai.tools.adapters.preparation import PreparationAdapter

    contacts = ContactsAdapter()
    calendar = CalendarAdapter()
    gmail = GmailAdapter()
    preparation = PreparationAdapter()
    return ToolRegistry.build(
        default_definitions(
            contacts=contacts,
            calendar=calendar,
            gmail=gmail,
            preparation=preparation,
        )
    )


__all__ = ["default_definitions", "default_registry"]