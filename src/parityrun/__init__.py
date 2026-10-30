"""parityrun: golden-recording parity tests for HTTP services.

Record every response, LLM prompt, outside-service call, database row and file a reference implementation produces
for a set of scenarios, then check any other implementation (a rewrite, a port, the next version) against them.
"""

from .project import Project, RunInfo, load
from .stubs import Stub

__all__ = ["Project", "RunInfo", "Stub", "load"]
__version__ = "0.4.0"
