"""Read Harbor selection and Fleet's DinD requests in the pinned worktree interpreter."""

import asyncio
import json
import sys

from fleet.harbor import HarborJobConfigSource, HarborWorkflowBuilder, PreparedHarborTask
from harbor.models.task.task import Task


async def requests(materialize, config):
    builder = HarborWorkflowBuilder.model_validate(config)
    tasks = {}
    for entry in [materialize, *(materialize.get("additional_jobs") or [])]:
        job = HarborJobConfigSource.model_validate(entry["job_source"]).load()
        selected = list(job.tasks)
        for dataset in job.datasets:
            if not dataset.is_local():
                raise ValueError("remote dataset")
            selected.extend(await dataset.get_task_configs(disable_verification=job.verifier.disable))
        for task in selected:
            if task.path is None:
                raise ValueError("task without local path")
            key = str(task.path)
            if key in tasks:
                continue
            env = Task(task.path, disable_verification=job.verifier.disable).config.environment
            # These are the fields the Harbor materializer passes to the builder.
            prepared = PreparedHarborTask.model_construct(cpus=env.cpus, memory_mb=env.memory_mb, storage_mb=env.storage_mb)
            dind = builder.dind_resource_strings(prepared)
            tasks[key] = [dind["cpu_request"], dind["memory_request"]]
    return {
        "selector": builder.node_selector, "tolerations": builder.tolerations,
        "runner": [builder.runner_cpu_request, builder.runner_memory_request], "tasks": tasks,
    }


if __name__ == "__main__":
    print(json.dumps(asyncio.run(requests(**json.load(sys.stdin)))))
