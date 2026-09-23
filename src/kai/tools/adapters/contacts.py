"""Contacts tool adapter.

``contacts.match`` reads the account's contacts through the existing
tenant-scoped service and filters by name/email/phone. The response surface
never carries the access token; only public contact fields are serialized.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from kai.contacts import client as contacts_client
from kai.contacts.service import (
    AccountNotFoundError,
    list_contacts,
    match_contacts,
)
from kai.tools.adapters.base import AdapterBase, provider_failure
from kai.tools.context import ToolContext
from kai.tools.errors import ToolExecutionError
from kai.tools.execution import ToolExecutionOutcome

_PROVIDER_CODES: dict[type[Exception], str] = {
    contacts_client.GoogleInvalidTokenError: "TOKEN_INVALID",
    contacts_client.GoogleForbiddenError: "PERMISSION_DENIED",
    contacts_client.GoogleRateLimitError: "RATE_LIMITED",
    contacts_client.GoogleApiError: "PROVIDER_ERROR",
}


def _serialize_contact(contact: Any) -> dict[str, Any]:
    return {
        "resource_name": contact.resource_name,
        "display_name": contact.display_name,
        "emails": list(contact.emails),
        "phone_numbers": list(contact.phone_numbers),
    }


class ContactsAdapter(AdapterBase):
    """Executes ``contacts.match`` against the stored Google account."""

    async def execute(
        self,
        *,
        tool: Any,
        arguments: Mapping[str, Any],
        context: ToolContext,
    ) -> ToolExecutionOutcome:
        if tool.name != "contacts.match":
            raise self.unimplemented(tool.name)
        self.record("contacts.match")
        transport = self.require_transport(context)
        try:
            contacts = await list_contacts(
                context.session,
                transport,
                account_id=self.require_account(context),
                tenant_id=context.tenant_id,
            )
        except AccountNotFoundError:
            raise ToolExecutionError(
                "google account not found for tenant", error_code="ACCOUNT_NOT_FOUND"
            ) from None
        except Exception as exc:
            raise provider_failure(exc, codes=_PROVIDER_CODES) from exc

        matched = match_contacts(contacts, arguments["query"])
        return ToolExecutionOutcome(
            ok=True,
            verified=True,
            data=[_serialize_contact(contact) for contact in matched],
            evidence={
                "total": len(contacts),
                "matched": len(matched),
                "query": arguments["query"],
            },
        )


__all__ = ["ContactsAdapter"]