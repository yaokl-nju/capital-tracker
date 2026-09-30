"""LLM service for making API calls with retry logic."""
import time
from typing import Optional, Dict, Any

from config.settings import LLMConfig
from core.diagnostics import record_event


class LLMService:
    """Service for LLM API calls with retry support."""

    def __init__(self, llm_config: LLMConfig, extra_body: Optional[Dict[str, Any]] = None):
        """
        Initialize LLM service.

        Args:
            llm_config: LLM configuration
            extra_body: Optional provider-specific request parameters
        """
        self.client = llm_config.create_client() if llm_config.api_key else None
        self.model = llm_config.model
        self.extra_body = extra_body

    def complete(
        self,
        prompt: str,
        system_prompt: str = "",
        response_format: Dict[str, str] = None,
        max_trials: int = 1
    ) -> Optional[str]:
        """
        Get LLM completion with retry logic.

        Args:
            prompt: User prompt
            system_prompt: Optional system prompt
            response_format: Response format (e.g., {"type": "json_object"})
            max_trials: Max number of trials

        Returns:
            LLM response text or None if failed
        """
        started = time.monotonic()
        if self.client is None:
            record_event('model', outcome='not_configured', duration_seconds=0.0)
            return None
        for attempt in range(max_trials):
            try:
                messages = []
                if system_prompt:
                    messages.append({"role": "system", "content": system_prompt})
                messages.append({"role": "user", "content": prompt})

                kwargs = {
                    "model": self.model,
                    "messages": messages,
                    "stream": False,
                }
                if response_format:
                    kwargs["response_format"] = response_format
                if self.extra_body:
                    kwargs["extra_body"] = self.extra_body

                response = self.client.chat.completions.create(**kwargs)
                content = response.choices[0].message.content
                if content and content.strip():
                    usage = getattr(response, 'usage', None)
                    counters = {}
                    for name in ('prompt_tokens', 'completion_tokens', 'total_tokens'):
                        value = getattr(usage, name, None)
                        if type(value) is int and value >= 0:
                            counters[name] = value
                    record_event('model', outcome='completed', duration_seconds=round(time.monotonic() - started, 3),
                                 **counters)
                    return content
                raise ValueError("Empty LLM response")

            except Exception as e:
                print(f"  [{self.model}] 请求失败: {type(e).__name__}")
                status = getattr(e, "status_code", None)
                record_event('model', outcome='error', error_type=type(e).__name__,
                             status_code=status if isinstance(status, int) else 0,
                             duration_seconds=round(time.monotonic() - started, 3))
                if status in (400, 401, 403, 404, 422):
                    break
                if attempt + 1 < max_trials:
                    time.sleep(1)

        return None
