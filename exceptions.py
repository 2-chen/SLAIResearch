"""
Custom exceptions for the SLAIResearch pipeline.
"""


class SLAIResearchError(Exception):
    """Base exception for all SLAIResearch errors."""
    pass


class CheckpointError(SLAIResearchError):
    """Error loading or saving checkpoint/state data."""
    pass


class StageError(SLAIResearchError):
    """Error in a specific pipeline stage."""
    def __init__(self, stage: str, message: str):
        self.stage = stage
        self.message = message
        super().__init__(f"[{stage}] {message}")


class LLMError(SLAIResearchError):
    """LLM call failed."""
    pass
