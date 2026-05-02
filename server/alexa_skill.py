"""Alexa custom skill wiring for the session-status service."""
from __future__ import annotations

from ask_sdk_core.dispatch_components import (
    AbstractExceptionHandler,
    AbstractRequestHandler,
)
from ask_sdk_core.skill_builder import SkillBuilder
from ask_sdk_core.utils import is_intent_name, is_request_type
from ask_sdk_webservice_support.webservice_handler import WebserviceSkillHandler

from status_store import StatusStore


class LaunchRequestHandler(AbstractRequestHandler):
    def __init__(self, store: StatusStore, limit: int) -> None:
        self.store = store
        self.limit = limit

    def can_handle(self, handler_input) -> bool:
        return is_request_type("LaunchRequest")(handler_input)

    def handle(self, handler_input):
        payload = self.store.voice_payload(limit=self.limit)
        speech = payload["speech_text"]
        return handler_input.response_builder.speak(speech).response


class GetSessionStatusIntentHandler(AbstractRequestHandler):
    def __init__(self, store: StatusStore, limit: int) -> None:
        self.store = store
        self.limit = limit

    def can_handle(self, handler_input) -> bool:
        return is_intent_name("GetSessionStatusIntent")(handler_input)

    def handle(self, handler_input):
        payload = self.store.voice_payload(limit=self.limit)
        speech = payload["speech_text"]
        return handler_input.response_builder.speak(speech).response


class HelpIntentHandler(AbstractRequestHandler):
    def can_handle(self, handler_input) -> bool:
        return is_intent_name("AMAZON.HelpIntent")(handler_input)

    def handle(self, handler_input):
        speech = (
            "Ask for your active coding sessions, and I will read the latest "
            "Claude and Codex status summaries."
        )
        return (
            handler_input.response_builder
            .speak(speech)
            .ask(speech)
            .response
        )


class CancelOrStopIntentHandler(AbstractRequestHandler):
    def can_handle(self, handler_input) -> bool:
        return (
            is_intent_name("AMAZON.CancelIntent")(handler_input)
            or is_intent_name("AMAZON.StopIntent")(handler_input)
        )

    def handle(self, handler_input):
        return handler_input.response_builder.speak("Okay.").response


class FallbackIntentHandler(AbstractRequestHandler):
    def can_handle(self, handler_input) -> bool:
        return is_intent_name("AMAZON.FallbackIntent")(handler_input)

    def handle(self, handler_input):
        speech = "Ask for your active sessions to hear the latest summaries."
        return handler_input.response_builder.speak(speech).ask(speech).response


class CatchAllExceptionHandler(AbstractExceptionHandler):
    def can_handle(self, handler_input, exception) -> bool:
        return True

    def handle(self, handler_input, exception):
        return handler_input.response_builder.speak(
            "I hit an error while checking your session status."
        ).response


def build_alexa_webservice_handler(
    store: StatusStore,
    skill_id: str | None,
    limit: int = 3,
    verify_signature: bool = True,
    verify_timestamp: bool = True,
) -> WebserviceSkillHandler:
    builder = SkillBuilder()
    if skill_id:
        builder.skill_id = skill_id
    builder.add_request_handler(LaunchRequestHandler(store, limit))
    builder.add_request_handler(GetSessionStatusIntentHandler(store, limit))
    builder.add_request_handler(HelpIntentHandler())
    builder.add_request_handler(CancelOrStopIntentHandler())
    builder.add_request_handler(FallbackIntentHandler())
    builder.add_exception_handler(CatchAllExceptionHandler())
    return WebserviceSkillHandler(
        skill=builder.create(),
        verify_signature=verify_signature,
        verify_timestamp=verify_timestamp,
    )
