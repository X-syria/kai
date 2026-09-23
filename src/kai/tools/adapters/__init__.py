"""Provider adapters: the only layer permitted to reach Google.

Adapters reuse the existing tenant-scoped integration services, so a tool run
traverses the exact same code path as the public API slices. The adapters add
deterministic error classification and JSON-safe serialization.
"""

__all__ = ["CalendarAdapter", "ContactsAdapter", "GmailAdapter"]