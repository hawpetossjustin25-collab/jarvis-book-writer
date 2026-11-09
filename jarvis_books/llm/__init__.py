"""Automatic LLM Adaptation Engine for Gemini (Justin's universal adaptive-LLM rule, fitted to this app)."""
from jarvis_books.llm.engine import AdaptiveLLMEngine
from jarvis_books.llm.models import LLMRequest, LLMResponse, PromptSection

__all__ = ["AdaptiveLLMEngine", "LLMRequest", "LLMResponse", "PromptSection"]
