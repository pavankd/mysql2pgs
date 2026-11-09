"""Schema Dump entry points: inspect a server, start/track/cancel dump jobs."""

from __future__ import annotations

from ..models import DumpConn, DumpRequest
from . import mssql, mysql, oracle, postgres
from .base import DumpError, DumpJob, dump_jobs, resolve_output, sql_comment_header

ENGINES = {"mysql": mysql, "postgres": postgres, "oracle": oracle, "mssql": mssql}
LABELS = {"mysql": "MySQL", "postgres": "PostgreSQL", "oracle": "Oracle", "mssql": "SQL Server"}


class DumpExists(DumpError):
    """The output file exists and overwrite was not requested."""


def inspect(c: DumpConn) -> dict:
    info = ENGINES[c.engine].inspect(c)
    # Internal per-schema details (e.g. MySQL dump flags) stay server-side.
    return {**info, "schemas": [{k: v for k, v in s.items() if not k.startswith("_")} for s in info["schemas"]]}


def start(req: DumpRequest) -> DumpJob:
    schemas = list(dict.fromkeys(s for s in req.schemas if s.strip()))
    if not schemas:
        raise DumpError("Select at least one schema")
    req = req.model_copy(update={"schemas": schemas})
    target = resolve_output(req.output_path)
    if target.is_dir():
        raise DumpError("The output path is a folder; add a file name")
    if target.exists() and not req.overwrite:
        raise DumpExists(f"{req.output_path} already exists. Choose another name or enable overwrite.")

    # Authoritative permission check: never trust the client's earlier view.
    module = ENGINES[req.connection.engine]
    info = module.inspect(req.connection)
    by_name = {s["name"]: s for s in info["schemas"]}
    unknown = [s for s in schemas if s not in by_name]
    if unknown:
        raise DumpError(f"Schema(s) not found or not visible to this user: {', '.join(unknown)}")
    problems = []
    for name in schemas:
        s = by_name[name]
        if req.content in ("schema", "both") and not s["can_dump_schema"]:
            problems.append(f"{name} (schema): {'; '.join(s['schema_issues'])}")
        if req.content in ("data", "both") and not s["can_dump_data"]:
            problems.append(f"{name} (data): {'; '.join(s['data_issues'])}")
    if problems:
        raise DumpError("Permission check failed. " + " | ".join(problems))

    job = DumpJob(request=req, target=target)

    def runner(job: DumpJob, writer) -> None:
        job.phase = "starting"
        job.log("info", f"Dumping {', '.join(schemas)} from {LABELS[req.connection.engine]} {info['version']} as {info['user']}")
        sql_comment_header(writer, LABELS[req.connection.engine], info["version"], job)
        module.dump(job, writer, info)

    return dump_jobs.start(job, runner)
