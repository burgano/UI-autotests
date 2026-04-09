import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class Job:
    id: str
    status: str = "pending"        # pending | running | done | error
    stage: str = ""                # Analyzing | Generating | Running | Done
    stage_index: int = 0           # 0-4 for progress bar
    logs: list = field(default_factory=list)
    results: Optional[dict] = None
    error: Optional[str] = None
    created_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    discovered_urls: list = field(default_factory=list)
    endpoint_statuses: dict = field(default_factory=dict)  # url -> "ok"|"skip"|"error"|"pending"


class JobManager:
    def __init__(self):
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    def create(self) -> Job:
        job = Job(id=str(uuid.uuid4()))
        with self._lock:
            self._jobs[job.id] = job
        return job

    def get(self, job_id: str) -> Optional[Job]:
        return self._jobs.get(job_id)

    def update(self, job_id: str, **kwargs):
        with self._lock:
            job = self._jobs.get(job_id)
            if job:
                for k, v in kwargs.items():
                    setattr(job, k, v)

    def append_log(self, job_id: str, line: str):
        with self._lock:
            job = self._jobs.get(job_id)
            if job:
                job.logs.append(line)


job_manager = JobManager()
