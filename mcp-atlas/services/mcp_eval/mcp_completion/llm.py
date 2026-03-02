"""LLM completion functionality using LiteLLM."""

import json
import logging
from typing import Any, Dict, List, Optional

import httpx
import litellm
from pydantic import BaseModel
from tenacity import retry, wait_exponential, stop_after_attempt, retry_if_exception_type

from .schema import Message, ToolCallSchema, AssistantMessage
from .config import config

logger = logging.getLogger(__name__)

# Configure LiteLLM - suppress verbose logging
litellm.set_verbose = False
logging.getLogger("LiteLLM").setLevel(logging.WARNING)


class LLMResponse(BaseModel):
    """Response from LLM completion."""

    message: AssistantMessage
    original_content: Optional[str] = None


def configure_litellm():
    litellm.api_base = config.LLM_BASE_URL  # could also be just openai url
    litellm.api_key = config.LLM_API_KEY


# Configure LiteLLM once at module level
configure_litellm()


def strip_all_additional_properties(schema: any) -> any:
    """Recursively remove all `additionalProperties` keys from the schema."""
    if isinstance(schema, dict):
        # Remove 'additionalProperties' if it exists
        schema.pop("additionalProperties", None)

        # Recurse into all values
        for key, value in schema.items():
            strip_all_additional_properties(value)

    elif isinstance(schema, list):
        for item in schema:
            strip_all_additional_properties(item)

    return schema


def _is_rate_limit_error(exception: Exception) -> bool:
    """Check if the exception is a rate limit error."""
    error_str = str(exception).lower()
    return (
        isinstance(exception, litellm.RateLimitError)
        or "rate limit" in error_str
        or "too many requests" in error_str
        or "429" in error_str
    )


@retry(
    retry=retry_if_exception_type(Exception),
    wait=wait_exponential(multiplier=1, min=5, max=60),
    stop=stop_after_attempt(5),
    reraise=True,
    before_sleep=lambda retry_state: logger.warning(
        f"Rate limit hit, retrying in {retry_state.next_action.sleep:.0f}s "
        f"(attempt {retry_state.attempt_number}/5)..."
    ),
)
async def create_completion(
    model: str,
    messages: List[Message],
    tools: List[ToolCallSchema],
) -> LLMResponse:
    """Create a completion using LiteLLM."""

    # Convert our schema to LiteLLM form at
    if "gemini" in model.lower():
        litellm_messages = [
            (
                msg.model_dump()
                if not isinstance(msg, AssistantMessage)
                else msg.original_message.model_dump()
            )
            for msg in messages
        ]
        litellm_tools = [
            strip_all_additional_properties(tool.model_dump()) for tool in tools
        ]
    else:
        litellm_messages = [msg.model_dump() for msg in messages]
        litellm_tools = [tool.model_dump() for tool in tools]

    try:
        response = await litellm.acompletion(
            model=model,
            messages=litellm_messages,
            tools=litellm_tools,
            api_key=config.LLM_API_KEY,
            api_base=config.LLM_BASE_URL,
            timeout=config.DEFAULT_TIMEOUT,
        )

        # Convert response back to our format
        # Handle tool_calls conversion from OpenAI format to our format
        tool_calls = None
        if response.choices[0].message.tool_calls:
            tool_calls = []
            for tool_call in response.choices[0].message.tool_calls:
                tool_calls.append(
                    {
                        "id": tool_call.id,
                        "type": tool_call.type,
                        "function": {
                            "name": tool_call.function.name,
                            "arguments": tool_call.function.arguments,
                        },
                    }
                )

        assistant_message = AssistantMessage(
            role="assistant",
            content=response.choices[0].message.content,
            tool_calls=tool_calls,
            original_message=response.choices[0].message,
        )

        return LLMResponse(message=assistant_message)

    except Exception as error:
        logger.error(f"LiteLLM completion failed: {error}")
        raise


def _transform_tool_calls(tools: List[Dict[str, Any]]) -> List[ToolCallSchema]:
    """Transform tool definitions to ToolCallSchema format."""
    return [
        ToolCallSchema(
            type="function",
            function={
                "name": tool["name"],
                "description": tool["description"],
                "parameters": tool.get("input_schema", {}),
                "strict": False,
            },
        )
        for tool in tools
    ]
