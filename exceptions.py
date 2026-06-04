"""
Custom exceptions for the ChenResearch pipeline.
"""


class ChenResearchError(Exception):
    """Base exception for all ChenResearch errors."""
    pass


class CheckpointError(ChenResearchError):
    """Error loading or saving checkpoint/state data."""
    pass


class StageError(ChenResearchError):
    """Error in a specific pipeline stage."""
    def __init__(self, stage: str, message: str):
        self.stage = stage
        self.message = message
        super().__init__(f"[{stage}] {message}")


class LLMError(ChenResearchError):
    """LLM call failed."""
    pass
