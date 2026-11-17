"""Small policy and adapter helpers for the WebView life assistant.

The native 2.2 chat window has two deliberately separate paths:

* ordinary chat is read-only and may use the knowledge-card retrieval wrapper;
* a life-context request is sent through :class:`ConversationAgentProvider`
  and produces a reviewable draft.  A draft is never a write operation.

Keeping the decision in this module gives the WebView bridge one place to
reuse the boundary without making the browser aware of the life-agent tool
protocol.  The bridge still owns authentication, consent persistence and the
call to ``create_member_draft``.
"""

from __future__ import annotations

import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from ..providers.base import ChatProvider
from ..services.chat_personalization import needs_life_context
from ..services.life_agent import ConversationAgentProvider

LifeChatMode = Literal["ordinary", "life_agent"]


@dataclass(frozen=True)
class LifeChatRoute:
    """The safe route selected for one member message.

    ``life_agent`` is selected only after the caller has verified both the
    member feature switch and the user's context-sharing consent.  The route
    object carries a reason for audit/debug UI, while keeping the policy
    decision independent from a particular bridge implementation.
    """

    mode: LifeChatMode
    reason: str

    @property
    def use_life_agent(self) -> bool:
        return self.mode == "life_agent"


def route_member_message(
    content: str,
    *,
    share_context: bool,
    member_assistant_enabled: bool = True,
    context_service: object | None = None,
) -> LifeChatRoute:
    """Choose ordinary chat or the reviewed life-agent path.

    This does *not* read a database, call a provider or persist consent.  A
    request becomes an agent request only when all of the following are true:

    * the text is a life-context request according to the shared bounded
      classifier;
    * the user explicitly allowed context sharing for this request; and
    * the member assistant is enabled and exposes ``create_member_draft``.

    A missing service or a disabled feature falls back to ordinary chat.  The
    caller may choose to reject that route instead, but it must never silently
    send a context snapshot when one of the gates is false.
    """

    if not isinstance(content, str) or not content.strip():
        return LifeChatRoute("ordinary", "empty_message")
    if not needs_life_context(content):
        return LifeChatRoute("ordinary", "general_question")
    if not share_context:
        return LifeChatRoute("ordinary", "context_consent_off")
    if not member_assistant_enabled:
        return LifeChatRoute("ordinary", "feature_disabled")
    if context_service is None or not callable(
        getattr(context_service, "create_member_draft", None)
    ):
        return LifeChatRoute("ordinary", "draft_service_unavailable")
    return LifeChatRoute("life_agent", "life_context_with_consent")


def build_agent_provider(
    provider: ChatProvider,
    history: Sequence[Mapping[str, Any]],
    current_message: Mapping[str, Any],
    cancel_event: threading.Event | None = None,
    *,
    input_source: str = "text",
    answer_settings: Mapping[str, Any] | None = None,
) -> ConversationAgentProvider:
    """Wrap an existing provider with the bounded 2.2 conversation agent.

    ``RemoteAiAssistantService`` performs the signed prepare/finish RPC.  It
    invokes ``provider.chat`` with the prepared messages, so callers must pass
    this wrapper there when they want tool-loop behaviour; passing the raw
    provider would produce an ordinary JSON response and skip the snapshot
    tools.  The wrapper itself performs no network or persistence work.
    """

    # ChatProvider is intentionally a lightweight protocol in practice;
    # adapters used by tests may implement it without subclassing.  Keep a
    # useful validation error while accepting duck-typed providers.
    if not isinstance(provider, ChatProvider) and not callable(
        getattr(provider, "chat", None)
    ):
        raise TypeError("AI provider must expose chat()")
    if not isinstance(current_message, Mapping):
        raise TypeError("current_message must be a mapping")
    if current_message.get("role") not in {"user", None}:
        raise ValueError("life-agent current message must be a user message")
    event = cancel_event if cancel_event is not None else threading.Event()
    return ConversationAgentProvider(
        provider,
        history,
        current_message,
        event,
        input_source=input_source,
        answer_settings=answer_settings,
    )


def normalize_input_source(
    *,
    has_images: bool = False,
    requested: str | None = None,
) -> str:
    """Return the provenance label accepted by the life-agent validator."""

    if has_images:
        return "photo"
    if isinstance(requested, str) and requested in {"text", "voice"}:
        return requested
    return "text"


__all__ = [
    "LifeChatRoute",
    "LifeChatMode",
    "build_agent_provider",
    "needs_life_context",
    "normalize_input_source",
    "route_member_message",
]
