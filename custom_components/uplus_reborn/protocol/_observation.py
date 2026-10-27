"""Optional passive observation of the existing local UWT exchange."""

from typing import Protocol


class ExchangeObserver(Protocol):
    """Receive synchronous observations without changing traffic or proof.

    Callers check ``active`` before constructing trace-only context. Implementors
    must keep these callbacks bounded and must not raise or perform network I/O.
    Raw input is a transport chunk, not necessarily one complete protocol frame.
    """

    @property
    def active(self) -> bool:
        """Whether new observations are currently wanted."""
        ...

    def event(self, event: str, **context: object) -> None:
        """Observe a processing decision or an already-decoded snapshot."""
        ...

    def raw(self, direction: str, data: bytes, *, stage: str) -> None:
        """Observe actual submitted write/read bytes, without copying here."""
        ...
