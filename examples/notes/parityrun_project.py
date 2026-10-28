"""The notes example as a parityrun project: `parityrun --project examples/notes/parityrun_project.py record`."""

import pathlib

from parityrun import Project, RunInfo


def env(run: RunInfo) -> dict:
    return {"NOTES_DATA": str(run.data_dir), "LLM_BASE_URL": f"{run.stub_url}/v1", "LLM_MODEL": "conformance"}


PROJECT = Project(
    root=pathlib.Path(__file__).parent,
    repo=pathlib.Path(__file__).parent,
    reference="python",
    quick=["notes"],
    env=env,
    ready="/health",
    shutdown="POST /shutdown",
    databases=["notes.db"],
)
