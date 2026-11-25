"""NEXUS: Jarvis's internet observation (OS1), intelligence (OS2) and tool-calling (OS3) systems.

OS1 is built: a persistent, searchable graph of web pages that Jarvis explores within set limits.
"""
from jarvis_books.nexus.config import CrawlConfig
from jarvis_books.nexus.contracts import ContractError, NexusRequest, NexusResponse
from jarvis_books.nexus.os1 import OS1

__all__ = ["OS1", "CrawlConfig", "NexusRequest", "NexusResponse", "ContractError"]
