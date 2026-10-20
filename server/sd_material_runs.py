#!/usr/bin/env python
"""
sd_material_runs.py - Versioned, auditable material-iteration runs (bridge side).

This module adds a deterministic workflow layer on top of the stateless MCP
tools: a *run* iterates a Substance Designer graph toward a goal, and every
step is persisted so the whole session can be replayed and audited.

Design constraints (see also the project README):

* Designer is reached ONLY through a `designer` callable with the signature
  ``designer(cmd_type: str, params: dict) -> dict``, raising ``DesignerError``
  on failure. Nothing in this module imports Qt, ``sd.api`` or FastMCP.
* State lives in SQLite (stdlib) next to the run's checkpoint files; no
  external services. Every operation is synchronous and the controller
  serialises all operations with one lock, so SD is never driven by two
  operations at once.
* Checkpoints are authoritative package copies on disk
  (``SDPackageMgr.saveCopyOfPackageAs``) written by the plugin command
  ``checkpoint_package``. ``restore_material_revision`` restores a checkpoint
  through ``restore_package`` (unload working package -> load checkpoint ->
  optionally save back over the working path) and re-fingerprints the graph.
  Checkpoints are only claimed ``verified`` after the stored structure hash
  was compared with a fresh fingerprint of the checkpoint file itself.
* The bridge never treats "applied" as "accepted": a patch is applied, then a
  *fresh* preview artifact must be captured, then an evaluation is recorded,
  then the candidate is explicitly accepted or rejected (rejection restores
  the revision the candidate was based on).

State machine::

    created -> baseline_ready -> candidate_proposed -> awaiting_evaluation
            -> evaluated -> baseline_ready ... (until the iteration budget
            is exhausted or finish_material_run is called)
    terminal: finished | cancelled | failed

Patch semantics: allowed operations are ``set_parameter`` and
``set_output_size`` (max PATCH_MAX_OPERATIONS per candidate). A patch that is
invalid, fails to apply, or produces no observable change is rejected and the
working graph is rolled back to the base revision checkpoint.

This module is dependency-free by design so tests can drive the full state
machine with a fake designer.
"""
import difflib
import hashlib
import json
import os
import re
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

__all__ = [
    "DesignerError", "RunError",
    "MaterialPatchOperation", "MaterialPatch",
    "MaterialRun", "MaterialRevision", "MaterialCandidate", "MaterialEvaluation",
    "MaterialRunStore", "MaterialRunController",
    "canonical_json", "structure_sha256", "fingerprint_sha256",
    "DEFAULT_PREVIEW_PRESET", "DEFAULT_MAX_ITERATIONS",
    "ALLOWED_PATCH_OPS", "ALLOWED_VALUE_TYPES", "PATCH_MAX_OPERATIONS",
]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Commands this module may issue. They are plugin commands, sent through the
#: bridge's `_request`; the plugin resolves graphs/packages itself.
CMD_CHECKPOINT = "checkpoint_package"
CMD_RESTORE = "restore_package"
CMD_FINGERPRINT = "graph_fingerprint"
CMD_VERIFY_CHECKPOINT = "verify_checkpoint"
CMD_GET_NODE_INFO = "get_node_info"
CMD_GET_GRAPH_INFO = "get_graph_info"
CMD_SET_PARAMETERS = "set_parameters"
CMD_SET_OUTPUT_SIZE = "set_graph_output_size"
CMD_INSPECT_OUTPUTS = "inspect_outputs"
CMD_SEND_3D = "send_to_3d_view"
CMD_SEND_3D_STATUS = "send_to_3d_view_status"
CMD_CAPTURE_3D = "capture_3d_view"
CMD_SCENE_INFO = "get_scene_info"

STATE_CREATED = "created"
STATE_BASELINE_READY = "baseline_ready"
STATE_CANDIDATE_PROPOSED = "candidate_proposed"
STATE_AWAITING_EVALUATION = "awaiting_evaluation"
STATE_EVALUATED = "evaluated"
STATE_FINISHED = "finished"
STATE_CANCELLED = "cancelled"
STATE_FAILED = "failed"

STATES = (
    STATE_CREATED, STATE_BASELINE_READY, STATE_CANDIDATE_PROPOSED,
    STATE_AWAITING_EVALUATION, STATE_EVALUATED, STATE_FINISHED,
    STATE_CANCELLED, STATE_FAILED,
)
TERMINAL_STATES = frozenset({STATE_FINISHED, STATE_CANCELLED, STATE_FAILED})

TRANSITIONS: Dict[str, frozenset] = {
    STATE_CREATED: frozenset({STATE_BASELINE_READY, STATE_FAILED, STATE_CANCELLED}),
    STATE_BASELINE_READY: frozenset({STATE_CANDIDATE_PROPOSED, STATE_FINISHED,
                                     STATE_CANCELLED, STATE_FAILED}),
    STATE_CANDIDATE_PROPOSED: frozenset({STATE_AWAITING_EVALUATION,
                                         STATE_BASELINE_READY, STATE_FINISHED,
                                         STATE_CANCELLED, STATE_FAILED}),
    STATE_AWAITING_EVALUATION: frozenset({STATE_EVALUATED, STATE_BASELINE_READY,
                                          STATE_CANCELLED, STATE_FAILED}),
    STATE_EVALUATED: frozenset({STATE_BASELINE_READY, STATE_FINISHED,
                                STATE_CANCELLED, STATE_FAILED}),
    STATE_FINISHED: frozenset(),
    STATE_CANCELLED: frozenset(),
    STATE_FAILED: frozenset(),
}

#: Candidate record status values.
CAND_PROPOSED = "proposed"
CAND_INVALID = "invalid"
CAND_APPLIED = "applied"
CAND_APPLY_FAILED = "apply_failed"
CAND_EVALUATED = "evaluated"
CAND_ACCEPTED = "accepted"
CAND_REJECTED = "rejected"
CAND_SUPERSEDED = "superseded"
PENDING_CANDIDATE_STATUSES = (CAND_PROPOSED, CAND_APPLIED, CAND_EVALUATED)

ALLOWED_PATCH_OPS = frozenset({"set_parameter", "set_output_size"})
ALLOWED_VALUE_TYPES = frozenset({
    "float", "int", "bool", "string",
    "float2", "float3", "float4", "color", "int2", "int3", "int4",
})
PATCH_MAX_OPERATIONS = 32
OUTPUT_SIZE_LOG2_RANGE = (6, 14)

DEFAULT_MAX_ITERATIONS = 8
DEFAULT_REGRESSION_THRESHOLD = 0.1

DEFAULT_PREVIEW_PRESET: Dict[str, Any] = {
    "output_usages": ["height", "normal", "ambientOcclusion", "baseColor",
                      "roughness", "metallic"],
    "max_size": 1024,
    "histogram_bins": 16,
    "include_3d": True,
    "wait_ms": 600,
    "refresh_first": True,
    "send_timeout_s": 90.0,
}

_METRICS = ("min", "max", "mean")


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class DesignerError(RuntimeError):
    """A Designer (SD plugin) call failed or is unavailable."""


class RunError(RuntimeError):
    """The requested run operation is invalid for the current state/inputs."""


# ---------------------------------------------------------------------------
# Hashing / canonicalisation helpers
# ---------------------------------------------------------------------------

def canonical_json(obj: Any) -> str:
    """Deterministic JSON for hashing (sorted keys, tight separators)."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, default=str)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fingerprint_sha256(fingerprint: Optional[dict]) -> Optional[str]:
    """Full hash of a fingerprint payload (includes node ids/positions)."""
    if not fingerprint:
        return None
    return sha256_bytes(canonical_json(fingerprint).encode("utf-8"))


def _node_base_label(node: dict) -> str:
    return canonical_json({
        "definition": node.get("definition"),
        "url": node.get("url"),
        "position": [round(float(c), 3) for c in (node.get("position") or [0.0, 0.0])],
        "label": node.get("label") or "",
        "inputs": sorted(
            [canonical_json([p.get("id"), p.get("value"), bool(p.get("connected"))])
             for p in node.get("inputs") or []]),
        "annotations": sorted(
            [canonical_json([p.get("id"), p.get("value")])
             for p in node.get("annotations") or []]),
        "is_output": bool(node.get("is_output")),
    })


def structure_sha256(fingerprint: Optional[dict], rounds: int = 4) -> Optional[str]:
    """Isomorphism-invariant digest of a graph fingerprint.

    Node ids are deliberately excluded: SD does not guarantee that node
    identifiers survive a package save/load cycle, so verification compares
    this digest (and reports id drift separately via fingerprint_sha256).

    The digest is computed with a few rounds of colour refinement
    (Weisfeiler-Lehman style) so node roles participate, then the multiset of
    final node labels and the labelled edge list are hashed. Graphs with
    exactly symmetric nodes may still collide; for material graphs this is not
    a practical concern and the full hash is stored alongside.
    """
    if not fingerprint:
        return None
    nodes = fingerprint.get("nodes") or []
    connections = fingerprint.get("connections") or []
    index = {}
    for i, node in enumerate(nodes):
        index.setdefault(node.get("id"), i)

    labels = [sha256_bytes(_node_base_label(n).encode("utf-8"))[:16] for n in nodes]
    in_edges: List[List[Any]] = [[] for _ in nodes]
    out_edges: List[List[Any]] = [[] for _ in nodes]
    for conn in connections:
        fi = index.get(conn.get("from_node"))
        ti = index.get(conn.get("to_node"))
        if fi is None or ti is None:
            continue
        out_edges[fi].append([conn.get("from_output") or "", labels[ti]])
        in_edges[ti].append([labels[fi], conn.get("to_input") or ""])

    for _ in range(max(1, int(rounds))):
        new_labels = []
        for i in range(len(nodes)):
            payload = canonical_json([
                labels[i],
                sorted(out_edges[i]),
                sorted(in_edges[i]),
            ])
            new_labels.append(sha256_bytes(payload.encode("utf-8"))[:16])
        labels = new_labels

    edges = []
    for conn in connections:
        fi = index.get(conn.get("from_node"))
        ti = index.get(conn.get("to_node"))
        if fi is None or ti is None:
            continue
        edges.append([labels[fi], conn.get("from_output") or "",
                      labels[ti], conn.get("to_input") or ""])

    graph_inputs = sorted(
        canonical_json([p.get("id"), p.get("value")])
        for p in fingerprint.get("graph_inputs") or [])
    payload = canonical_json({
        "labels": sorted(labels),
        "edges": sorted(edges),
        "graph_inputs": graph_inputs,
    })
    return sha256_bytes(payload.encode("utf-8"))


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

_UNSET = object()


@dataclass
class MaterialPatchOperation:
    op: str = ""
    node_id: Optional[str] = None
    parameter_id: Optional[str] = None
    value: Any = _UNSET
    value_type: Optional[str] = None
    width_log2: Optional[int] = None
    height_log2: Optional[int] = None

    @classmethod
    def from_dict(cls, data: dict) -> "MaterialPatchOperation":
        if not isinstance(data, dict):
            raise RunError("Each patch operation must be an object (got {!r}).".format(data))
        known = {field.name for field in cls.__dataclass_fields__.values()}
        unknown = sorted(set(data) - known - {"op"})
        if unknown:
            raise RunError("Unknown operation field(s): {}. Allowed: {}.".format(
                ", ".join(unknown), ", ".join(sorted(known - {"op"}))))
        kwargs: Dict[str, Any] = {}
        for name in ("op", "node_id", "parameter_id", "value_type",
                     "width_log2", "height_log2"):
            if name in data:
                kwargs[name] = data[name]
        if "value" in data:
            kwargs["value"] = data["value"]
        return cls(**kwargs)

    def to_dict(self) -> dict:
        out: Dict[str, Any] = {"op": self.op}
        if self.node_id is not None:
            out["node_id"] = self.node_id
        if self.parameter_id is not None:
            out["parameter_id"] = self.parameter_id
        if self.value is not _UNSET:
            out["value"] = self.value
        if self.value_type is not None:
            out["value_type"] = self.value_type
        if self.width_log2 is not None:
            out["width_log2"] = self.width_log2
        if self.height_log2 is not None:
            out["height_log2"] = self.height_log2
        return out


@dataclass
class MaterialPatch:
    operations: List[MaterialPatchOperation] = field(default_factory=list)
    notes: str = ""

    @classmethod
    def from_operations(cls, operations: List[dict], notes: str = "") -> "MaterialPatch":
        if not isinstance(operations, list):
            raise RunError("'operations' must be a list of patch operation objects.")
        return cls(operations=[MaterialPatchOperation.from_dict(op) for op in operations],
                   notes=notes or "")

    def to_dict(self) -> dict:
        return {"operations": [op.to_dict() for op in self.operations],
                "notes": self.notes}


@dataclass
class MaterialRun:
    run_id: str
    created_at: float
    updated_at: float
    state: str
    graph_identifier: str
    package_index: int
    package_path: Optional[str]
    goal: str
    max_iterations: int
    iteration: int
    baseline_revision_id: Optional[str]
    current_revision_id: Optional[str]
    best_revision_id: Optional[str]
    preview_preset: dict
    constraints: list
    seeds: dict
    transitions: list
    report: Optional[dict]
    finished_at: Optional[float]
    error: Optional[str]


@dataclass
class MaterialRevision:
    revision_id: str
    run_id: str
    seq: int
    created_at: float
    parent_revision_id: Optional[str]
    label: str
    package_index: int
    package_path: Optional[str]
    checkpoint_path: Optional[str]
    checkpoint_sha256: Optional[str]
    fingerprint: Optional[dict]
    structure_sha256: Optional[str]
    full_sha256: Optional[str]
    output_config: dict
    parameters: dict
    notes: str
    verified: Optional[bool] = None
    verified_at: Optional[float] = None


@dataclass
class MaterialCandidate:
    candidate_id: str
    run_id: str
    revision_id: str
    created_at: float
    patch: dict
    validation: dict
    status: str
    apply_result: Optional[dict]
    applied_at: Optional[float]
    decided_at: Optional[float]
    decision: Optional[dict]
    error: Optional[str]


@dataclass
class MaterialEvaluation:
    evaluation_id: str
    run_id: str
    candidate_id: str
    revision_id: str
    created_at: float
    evaluator: str
    summary: str
    verdict: str
    accepted: bool
    score: Optional[float]
    criteria: list
    regression_threshold: float
    baseline_revision_id: Optional[str]
    comparisons: dict
    technical_checks: list
    technical_passed: bool
    warnings: list
    artifact_id: Optional[int]


# ---------------------------------------------------------------------------
# SQLite store
# ---------------------------------------------------------------------------

_RUN_COLUMNS = (
    "run_id", "created_at", "updated_at", "state", "graph_identifier",
    "package_index", "package_path", "goal", "max_iterations", "iteration",
    "baseline_revision_id", "current_revision_id", "best_revision_id",
    "preview_preset", "constraints", "seeds", "transitions", "report",
    "finished_at", "error",
)
_REVISION_COLUMNS = (
    "revision_id", "run_id", "seq", "created_at", "parent_revision_id",
    "label", "package_index", "package_path", "checkpoint_path",
    "checkpoint_sha256", "fingerprint", "structure_sha256", "full_sha256",
    "output_config", "parameters", "notes", "verified", "verified_at",
)
_CANDIDATE_COLUMNS = (
    "candidate_id", "run_id", "revision_id", "created_at", "patch",
    "validation", "status", "apply_result", "applied_at", "decided_at",
    "decision", "error",
)
_EVALUATION_COLUMNS = (
    "evaluation_id", "run_id", "candidate_id", "revision_id", "created_at",
    "evaluator", "summary", "verdict", "accepted", "score", "criteria",
    "regression_threshold", "baseline_revision_id", "comparisons",
    "technical_checks", "technical_passed", "warnings", "artifact_id",
)
_ARTIFACT_COLUMNS = (
    "artifact_id", "run_id", "revision_id", "kind", "path", "sha256",
    "created_at", "config", "stats", "source_fingerprint", "status", "error",
)


class MaterialRunStore:
    """SQLite persistence for runs, revisions, candidates, evaluations and
    preview artifacts. One short-lived connection per call (WAL mode), so any
    thread may call it; the controller still serialises *operations*."""

    def __init__(self, db_path: str):
        self.db_path = os.path.abspath(db_path)
        parent = os.path.dirname(self.db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ── generic helpers ──────────────────────────────────────────────────
    @staticmethod
    def _encode(value: Any) -> Any:
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, (dict, list)):
            return json.dumps(value, default=str)
        return value

    @staticmethod
    def _decode(row: sqlite3.Row, json_fields: tuple) -> dict:
        record = {}
        for key in row.keys():
            value = row[key]
            if key in json_fields and value is not None:
                try:
                    value = json.loads(value)
                except (TypeError, ValueError):
                    pass
            record[key] = value
        return record

    # ── runs ─────────────────────────────────────────────────────────────
    def insert_run(self, run: MaterialRun) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO runs ({}) VALUES ({})".format(
                    ", ".join(_RUN_COLUMNS), ", ".join("?" * len(_RUN_COLUMNS))),
                [self._encode(getattr(run, col)) for col in _RUN_COLUMNS])

    def update_run(self, run_id: str, **fields: Any) -> None:
        allowed = set(_RUN_COLUMNS) - {"run_id", "created_at"}
        update = {k: v for k, v in fields.items() if k in allowed}
        if not update:
            return
        update.setdefault("updated_at", time.time())
        assignments = ", ".join("{} = ?".format(k) for k in update)
        with self._connect() as conn:
            conn.execute("UPDATE runs SET {} WHERE run_id = ?".format(assignments),
                         [self._encode(v) for v in update.values()] + [run_id])

    def get_run(self, run_id: str) -> Optional[MaterialRun]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM runs WHERE run_id = ?",
                               (run_id,)).fetchone()
        return self._row_to_run(row) if row else None

    def latest_run(self) -> Optional[MaterialRun]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM runs ORDER BY created_at DESC, rowid DESC LIMIT 1"
            ).fetchone()
        return self._row_to_run(row) if row else None

    def list_runs(self, limit: int = 20) -> List[MaterialRun]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM runs ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (max(1, int(limit)),)).fetchall()
        return [self._row_to_run(row) for row in rows]

    def _row_to_run(self, row: sqlite3.Row) -> MaterialRun:
        record = self._decode(row, ("preview_preset", "constraints", "seeds",
                                    "transitions", "report"))
        return MaterialRun(**{key: record.get(key) for key in _RUN_COLUMNS})

    # ── revisions ────────────────────────────────────────────────────────
    def next_revision_seq(self, run_id: str) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 AS n FROM revisions WHERE run_id = ?",
                (run_id,)).fetchone()
        return int(row["n"])

    def insert_revision(self, revision: MaterialRevision) -> None:
        values = []
        for col in _REVISION_COLUMNS:
            value = getattr(revision, col)
            values.append(self._encode(value))
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO revisions ({}) VALUES ({})".format(
                    ", ".join(_REVISION_COLUMNS), ", ".join("?" * len(_REVISION_COLUMNS))),
                values)

    def get_revision(self, revision_id: str) -> Optional[MaterialRevision]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM revisions WHERE revision_id = ?",
                               (revision_id,)).fetchone()
        return self._row_to_revision(row) if row else None

    def list_revisions(self, run_id: str) -> List[MaterialRevision]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM revisions WHERE run_id = ? ORDER BY seq ASC",
                (run_id,)).fetchall()
        return [self._row_to_revision(row) for row in rows]

    def update_revision(self, revision_id: str, **fields: Any) -> None:
        allowed = set(_REVISION_COLUMNS) - {"revision_id", "run_id", "seq"}
        update = {k: v for k, v in fields.items() if k in allowed}
        if not update:
            return
        assignments = ", ".join("{} = ?".format(k) for k in update)
        with self._connect() as conn:
            conn.execute("UPDATE revisions SET {} WHERE revision_id = ?".format(assignments),
                         [self._encode(v) for v in update.values()] + [revision_id])

    def _row_to_revision(self, row: sqlite3.Row) -> MaterialRevision:
        record = self._decode(row, ("fingerprint", "output_config", "parameters"))
        return MaterialRevision(**{key: record.get(key) for key in _REVISION_COLUMNS})

    # ── candidates ───────────────────────────────────────────────────────
    def insert_candidate(self, candidate: MaterialCandidate) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO candidates ({}) VALUES ({})".format(
                    ", ".join(_CANDIDATE_COLUMNS), ", ".join("?" * len(_CANDIDATE_COLUMNS))),
                [self._encode(getattr(candidate, col)) for col in _CANDIDATE_COLUMNS])

    def get_candidate(self, candidate_id: str) -> Optional[MaterialCandidate]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM candidates WHERE candidate_id = ?",
                               (candidate_id,)).fetchone()
        return self._row_to_candidate(row) if row else None

    def list_candidates(self, run_id: str) -> List[MaterialCandidate]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM candidates WHERE run_id = ? ORDER BY created_at ASC, rowid ASC",
                (run_id,)).fetchall()
        return [self._row_to_candidate(row) for row in rows]

    def update_candidate(self, candidate_id: str, **fields: Any) -> None:
        allowed = set(_CANDIDATE_COLUMNS) - {"candidate_id", "run_id", "created_at"}
        update = {k: v for k, v in fields.items() if k in allowed}
        if not update:
            return
        assignments = ", ".join("{} = ?".format(k) for k in update)
        with self._connect() as conn:
            conn.execute("UPDATE candidates SET {} WHERE candidate_id = ?".format(assignments),
                         [self._encode(v) for v in update.values()] + [candidate_id])

    def _row_to_candidate(self, row: sqlite3.Row) -> MaterialCandidate:
        record = self._decode(row, ("patch", "validation", "apply_result", "decision"))
        return MaterialCandidate(**{key: record.get(key) for key in _CANDIDATE_COLUMNS})

    # ── evaluations ──────────────────────────────────────────────────────
    def insert_evaluation(self, evaluation: MaterialEvaluation) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO evaluations ({}) VALUES ({})".format(
                    ", ".join(_EVALUATION_COLUMNS),
                    ", ".join("?" * len(_EVALUATION_COLUMNS))),
                [self._encode(getattr(evaluation, col)) for col in _EVALUATION_COLUMNS])

    def list_evaluations(self, run_id: str) -> List[MaterialEvaluation]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM evaluations WHERE run_id = ? ORDER BY created_at ASC, rowid ASC",
                (run_id,)).fetchall()
        return [self._row_to_evaluation(row) for row in rows]

    def latest_evaluation(self, candidate_id: str) -> Optional[MaterialEvaluation]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM evaluations WHERE candidate_id = ? "
                "ORDER BY created_at DESC, rowid DESC LIMIT 1",
                (candidate_id,)).fetchone()
        return self._row_to_evaluation(row) if row else None

    def _row_to_evaluation(self, row: sqlite3.Row) -> MaterialEvaluation:
        record = self._decode(row, ("criteria", "comparisons", "technical_checks",
                                    "warnings"))
        record["accepted"] = bool(record.get("accepted"))
        record["technical_passed"] = bool(record.get("technical_passed"))
        return MaterialEvaluation(**{key: record.get(key) for key in _EVALUATION_COLUMNS})

    # ── artifacts ────────────────────────────────────────────────────────
    def insert_artifact(self, *, run_id: str, revision_id: str, kind: str,
                        path: Optional[str], sha256: Optional[str],
                        config: dict, stats: Any, source_fingerprint: Optional[str],
                        status: str = "ok", error: Optional[str] = None,
                        created_at: Optional[float] = None) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                "INSERT INTO artifacts ({}) VALUES ({})".format(
                    ", ".join(_ARTIFACT_COLUMNS), ", ".join("?" * len(_ARTIFACT_COLUMNS))),
                [self._encode(None if key == "artifact_id" else value)
                 for key, value in (
                     ("artifact_id", None),
                     ("run_id", run_id),
                     ("revision_id", revision_id),
                     ("kind", kind),
                     ("path", path),
                     ("sha256", sha256),
                     ("created_at", created_at if created_at is not None else time.time()),
                     ("config", config),
                     ("stats", stats),
                     ("source_fingerprint", source_fingerprint),
                     ("status", status),
                     ("error", error),
                 )])
            return int(cursor.lastrowid)

    def list_artifacts(self, run_id: str,
                       revision_id: Optional[str] = None) -> List[dict]:
        query = "SELECT * FROM artifacts WHERE run_id = ?"
        args: List[Any] = [run_id]
        if revision_id:
            query += " AND revision_id = ?"
            args.append(revision_id)
        query += " ORDER BY created_at ASC, rowid ASC"
        with self._connect() as conn:
            rows = conn.execute(query, args).fetchall()
        return [self._decode(row, ("config", "stats")) for row in rows]

    def latest_artifact(self, run_id: str, kind: Optional[str] = None,
                        revision_id: Optional[str] = None) -> Optional[dict]:
        query = "SELECT * FROM artifacts WHERE run_id = ?"
        args: List[Any] = [run_id]
        if kind:
            query += " AND kind = ?"
            args.append(kind)
        if revision_id:
            query += " AND revision_id = ?"
            args.append(revision_id)
        query += " ORDER BY created_at DESC, rowid DESC LIMIT 1"
        with self._connect() as conn:
            row = conn.execute(query, args).fetchone()
        return self._decode(row, ("config", "stats")) if row else None


_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    state TEXT NOT NULL,
    graph_identifier TEXT NOT NULL,
    package_index INTEGER NOT NULL DEFAULT 0,
    package_path TEXT,
    goal TEXT NOT NULL DEFAULT '',
    max_iterations INTEGER NOT NULL,
    iteration INTEGER NOT NULL DEFAULT 0,
    baseline_revision_id TEXT,
    current_revision_id TEXT,
    best_revision_id TEXT,
    preview_preset TEXT,
    constraints TEXT,
    seeds TEXT,
    transitions TEXT,
    report TEXT,
    finished_at REAL,
    error TEXT
);
CREATE TABLE IF NOT EXISTS revisions (
    revision_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    created_at REAL NOT NULL,
    parent_revision_id TEXT,
    label TEXT,
    package_index INTEGER,
    package_path TEXT,
    checkpoint_path TEXT,
    checkpoint_sha256 TEXT,
    fingerprint TEXT,
    structure_sha256 TEXT,
    full_sha256 TEXT,
    output_config TEXT,
    parameters TEXT,
    notes TEXT,
    verified INTEGER,
    verified_at REAL,
    UNIQUE (run_id, seq)
);
CREATE TABLE IF NOT EXISTS candidates (
    candidate_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    revision_id TEXT NOT NULL,
    created_at REAL NOT NULL,
    patch TEXT,
    validation TEXT,
    status TEXT NOT NULL,
    apply_result TEXT,
    applied_at REAL,
    decided_at REAL,
    decision TEXT,
    error TEXT
);
CREATE TABLE IF NOT EXISTS evaluations (
    evaluation_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    candidate_id TEXT NOT NULL,
    revision_id TEXT NOT NULL,
    created_at REAL NOT NULL,
    evaluator TEXT,
    summary TEXT,
    verdict TEXT,
    accepted INTEGER NOT NULL DEFAULT 0,
    score REAL,
    criteria TEXT,
    regression_threshold REAL,
    baseline_revision_id TEXT,
    comparisons TEXT,
    technical_checks TEXT,
    technical_passed INTEGER NOT NULL DEFAULT 0,
    warnings TEXT,
    artifact_id INTEGER
);
CREATE TABLE IF NOT EXISTS artifacts (
    artifact_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    revision_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    path TEXT,
    sha256 TEXT,
    created_at REAL NOT NULL,
    config TEXT,
    stats TEXT,
    source_fingerprint TEXT,
    status TEXT NOT NULL DEFAULT 'ok',
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_revisions_run ON revisions(run_id, seq);
CREATE INDEX IF NOT EXISTS idx_candidates_run ON candidates(run_id);
CREATE INDEX IF NOT EXISTS idx_evaluations_run ON evaluations(run_id);
CREATE INDEX IF NOT EXISTS idx_artifacts_run ON artifacts(run_id, kind);
"""


# ---------------------------------------------------------------------------
# Controller
# ---------------------------------------------------------------------------

def _now() -> float:
    return time.time()


def _is_inside(path: Optional[str], directory: str) -> bool:
    if not path:
        return False
    try:
        path_abs = os.path.abspath(path)
        dir_abs = os.path.abspath(directory)
        return os.path.commonpath([path_abs, dir_abs]) == dir_abs
    except (ValueError, OSError):
        return False


def _norm_usage(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (value or "").lower())


class MaterialRunController:
    """State machine + orchestration for material-iteration runs.

    All public methods serialise on one lock: the controller never issues two
    Designer commands concurrently, and run records are only mutated by one
    operation at a time. The controller is stateless between calls apart from
    the store, so a bridge restart resumes runs transparently.
    """

    def __init__(self, store: MaterialRunStore, designer: Callable[[str, dict], dict],
                 runs_dir: str, clock: Callable[[], float] = _now):
        self.store = store
        self.designer = designer
        self.runs_dir = os.path.abspath(runs_dir)
        os.makedirs(self.runs_dir, exist_ok=True)
        self._clock = clock
        self._lock = threading.RLock()

    # ══════════════════════════════════════════════════════════════════
    # Low-level helpers
    # ══════════════════════════════════════════════════════════════════

    def _call(self, cmd_type: str, params: dict) -> dict:
        try:
            result = self.designer(cmd_type, params)
        except DesignerError:
            raise
        except Exception as exc:  # bridge ToolError / unexpected
            raise DesignerError("{} failed: {}".format(cmd_type, exc)) from exc
        if not isinstance(result, dict):
            raise DesignerError("{} returned {!r}, expected an object.".format(
                cmd_type, result))
        return result

    def _call_optional(self, cmd_type: str, params: dict) -> tuple:
        """Returns (result, error_message); never raises."""
        try:
            return self._call(cmd_type, params), None
        except DesignerError as exc:
            return None, str(exc)

    def _run_dir(self, run_id: str) -> str:
        path = os.path.join(self.runs_dir, run_id)
        os.makedirs(path, exist_ok=True)
        return path

    def _require_run(self, run_id: Optional[str]) -> MaterialRun:
        run = self.store.get_run(run_id) if run_id else self.store.latest_run()
        if run is None:
            raise RunError("Run '{}' not found.".format(run_id) if run_id
                           else "No material runs exist yet. Call start_material_run first.")
        return run

    def _require_revision(self, run: MaterialRun, revision_id: Optional[str]) -> MaterialRevision:
        revision_id = revision_id or run.current_revision_id
        if not revision_id:
            raise RunError("Run '{}' has no checkpoint yet. Call "
                           "checkpoint_material_revision first.".format(run.run_id))
        revision = self.store.get_revision(revision_id)
        if revision is None or revision.run_id != run.run_id:
            raise RunError("Revision '{}' not found in run '{}'.".format(
                revision_id, run.run_id))
        return revision

    def _transition(self, run: MaterialRun, target: str, reason: str) -> MaterialRun:
        if run.state == target:
            return run
        allowed = TRANSITIONS.get(run.state, frozenset())
        if target not in allowed:
            raise RunError(
                "Illegal state transition {} -> {} for run '{}' ({}).".format(
                    run.state, target, run.run_id, reason))
        transitions = list(run.transitions or [])
        transitions.append({"at": self._clock(), "from": run.state,
                            "to": target, "reason": reason})
        self.store.update_run(run.run_id, state=target, transitions=transitions)
        return self.store.get_run(run.run_id)

    def _pending_candidate(self, run: MaterialRun) -> Optional[MaterialCandidate]:
        for candidate in reversed(self.store.list_candidates(run.run_id)):
            if candidate.status in PENDING_CANDIDATE_STATUSES:
                return candidate
        return None

    def _fingerprint(self, run: MaterialRun) -> dict:
        payload = self._call(CMD_FINGERPRINT,
                             {"graph_identifier": run.graph_identifier})
        if not payload.get("nodes") and not payload.get("connections"):
            raise DesignerError(
                "graph_fingerprint returned no node data for graph '{}'. "
                "Is the graph still in the package?".format(run.graph_identifier))
        return payload

    def _refresh_package_index(self, run: MaterialRun, package_path: Optional[str]) -> None:
        """After load/unload the package list order can change; re-locate the
        working package by graph or file path and persist its new index."""
        result, error = self._call_optional(CMD_SCENE_INFO, {})
        if error or not result:
            return
        packages = result.get("packages") or []
        target_index = None
        for entry in packages:
            if entry.get("file_path") and package_path and \
                    os.path.normcase(os.path.abspath(entry["file_path"])) == \
                    os.path.normcase(os.path.abspath(package_path)):
                target_index = entry.get("index")
                break
        if target_index is None:
            for entry in packages:
                graphs = entry.get("graphs") or []
                names = [g.get("identifier") if isinstance(g, dict) else g
                         for g in graphs]
                if run.graph_identifier in names:
                    target_index = entry.get("index")
                    break
        if target_index is not None and target_index != run.package_index:
            self.store.update_run(run.run_id, package_index=target_index)

    def _restore_to_revision(self, run: MaterialRun, revision: MaterialRevision,
                             save_back: Optional[bool] = None) -> dict:
        """Restore a revision's checkpoint; returns the plugin result."""
        if not revision.checkpoint_path:
            raise RunError("Revision '{}' has no checkpoint file; it cannot be "
                           "restored.".format(revision.revision_id))
        if not os.path.isfile(revision.checkpoint_path):
            raise RunError("Checkpoint file for revision '{}' is missing: {}".format(
                revision.revision_id, revision.checkpoint_path))
        if revision.checkpoint_sha256:
            actual_sha = sha256_file(revision.checkpoint_path)
            if actual_sha != revision.checkpoint_sha256:
                raise RunError(
                    "Checkpoint file for revision '{}' no longer matches its "
                    "recorded sha256 (expected {}, got {}). Run "
                    "verify_material_checkpoint or restore another revision.".format(
                        revision.revision_id, revision.checkpoint_sha256, actual_sha))
        if save_back is None:
            save_back = not _is_inside(run.package_path, self.runs_dir)
        result = self._call(CMD_RESTORE, {
            "checkpoint_path": revision.checkpoint_path,
            "package_index": run.package_index,
            "package_path": run.package_path,
            "save_back": bool(save_back),
            "graph_identifier": run.graph_identifier,
        })
        new_path = result.get("package_path") or run.package_path
        self._refresh_package_index(run, new_path)
        refreshed = self.store.get_run(run.run_id)
        if refreshed is not None and refreshed.package_path != new_path:
            self.store.update_run(run.run_id, package_path=new_path)
        return result

    def _drift(self, run: MaterialRun, revision: MaterialRevision,
               fingerprint: Optional[dict] = None) -> Optional[dict]:
        """Return a description when the working graph no longer matches the
        revision's fingerprint (manual edits, wrong graph tab, etc.)."""
        if not revision.structure_sha256:
            return None
        fingerprint = fingerprint if fingerprint is not None else self._fingerprint(run)
        live_structure = structure_sha256(fingerprint)
        live_full = fingerprint_sha256(fingerprint)
        if live_structure == revision.structure_sha256 and \
                live_full == revision.full_sha256:
            return None
        return {
            "revision_id": revision.revision_id,
            "expected_structure_sha256": revision.structure_sha256,
            "actual_structure_sha256": live_structure,
            "expected_full_sha256": revision.full_sha256,
            "actual_full_sha256": live_full,
            "structure_changed": live_structure != revision.structure_sha256,
        }

    def _assert_no_drift(self, run: MaterialRun,
                         revision: MaterialRevision) -> dict:
        fingerprint = self._fingerprint(run)
        drift = self._drift(run, revision, fingerprint)
        if drift:
            raise RunError(
                "The working graph does not match revision '{}' "
                "(structure_changed={}). If the current graph is worth keeping, "
                "call checkpoint_material_revision to record it first; otherwise "
                "call restore_material_revision to return to a recorded state. "
                "Details: {}".format(revision.revision_id, drift["structure_changed"],
                                     canonical_json(drift)))
        return fingerprint

    # ══════════════════════════════════════════════════════════════════
    # Public operations
    # ══════════════════════════════════════════════════════════════════

    def start_run(self, graph_identifier: str,
                  goal: str = "",
                  max_iterations: int = DEFAULT_MAX_ITERATIONS,
                  preview_preset: Optional[dict] = None,
                  constraints: Optional[list] = None,
                  seeds: Optional[dict] = None,
                  package_index: int = 0,
                  package_path: Optional[str] = None) -> dict:
        with self._lock:
            if not graph_identifier or not isinstance(graph_identifier, str):
                raise RunError("graph_identifier is required.")
            try:
                max_iterations = int(max_iterations)
            except (TypeError, ValueError):
                raise RunError("max_iterations must be an integer.")
            if max_iterations < 1:
                raise RunError("max_iterations must be >= 1.")

            preset = dict(DEFAULT_PREVIEW_PRESET)
            if preview_preset:
                if not isinstance(preview_preset, dict):
                    raise RunError("preview_preset must be an object.")
                unknown = sorted(set(preview_preset) - set(DEFAULT_PREVIEW_PRESET))
                if unknown:
                    raise RunError("Unknown preview_preset key(s): {}. Allowed: {}.".format(
                        ", ".join(unknown), ", ".join(sorted(DEFAULT_PREVIEW_PRESET))))
                preset.update(preview_preset)

            checks, check_errors = self._validate_constraints(constraints or [])
            if check_errors:
                raise RunError("Invalid constraints: " + "; ".join(check_errors))

            # Fail fast if the graph is not resolvable.
            scene, error = self._call_optional(
                CMD_GET_GRAPH_INFO,
                {"graph_identifier": graph_identifier, "node_limit": 0})
            if error:
                raise RunError("Cannot find graph '{}': {}".format(graph_identifier, error))

            run_id = uuid.uuid4().hex[:12]
            now = self._clock()
            run = MaterialRun(
                run_id=run_id, created_at=now, updated_at=now,
                state=STATE_CREATED, graph_identifier=graph_identifier,
                package_index=int(package_index or 0), package_path=package_path,
                goal=goal or "", max_iterations=max_iterations, iteration=0,
                baseline_revision_id=None, current_revision_id=None,
                best_revision_id=None, preview_preset=preset,
                constraints=checks, seeds=dict(seeds or {}),
                transitions=[{"at": now, "from": None, "to": STATE_CREATED,
                              "reason": "run created"}],
                report=None, finished_at=None, error=None)
            self.store.insert_run(run)
            return {"run": self._public_run(self.store.get_run(run_id))}

    def get_run(self, run_id: Optional[str] = None,
                include_artifacts: bool = True) -> dict:
        with self._lock:
            run = self._require_run(run_id)
            return self._run_bundle(run, include_artifacts=include_artifacts)

    def list_runs(self, limit: int = 20) -> dict:
        with self._lock:
            runs = self.store.list_runs(limit)
            return {"runs": [self._public_run(run, brief=True) for run in runs],
                    "count": len(runs)}

    def checkpoint_revision(self, run_id: Optional[str] = None,
                            label: Optional[str] = None,
                            checkpoint_path: Optional[str] = None,
                            verify: bool = True,
                            notes: Optional[str] = None) -> dict:
        with self._lock:
            run = self._require_run(run_id)
            if run.state in TERMINAL_STATES:
                raise RunError("Run '{}' is {}; no further checkpoints.".format(
                    run.run_id, run.state))
            if self._pending_candidate(run):
                raise RunError("A candidate is pending; apply it or decide it "
                               "before creating another checkpoint.")
            revision = self._create_revision(run, label=label,
                                             checkpoint_path=checkpoint_path,
                                             verify=verify, notes=notes)
            run = self.store.get_run(run.run_id)
            if run.state == STATE_CREATED:
                run = self._transition(run, STATE_BASELINE_READY,
                                       "baseline revision {}".format(revision.revision_id))
            return {"run": self._public_run(run),
                    "revision": self._public_revision(revision)}

    def _create_revision(self, run: MaterialRun, label: Optional[str] = None,
                         checkpoint_path: Optional[str] = None,
                         verify: bool = True,
                         notes: Optional[str] = None) -> MaterialRevision:
        """Save an authoritative checkpoint of the working package and record
        it as a revision. The caller decides the run's state transitions; this
        helper does not check for pending candidates, so it is also used to
        auto-checkpoint an applied candidate."""
        seq = self.store.next_revision_seq(run.run_id)
        if not checkpoint_path:
            checkpoint_path = os.path.join(self._run_dir(run.run_id), "revisions",
                                           "r{:04d}.sbs".format(seq))
        checkpoint = self._call(CMD_CHECKPOINT, {
            "package_index": run.package_index,
            "package_path": run.package_path,
            "checkpoint_path": checkpoint_path,
            "graph_identifier": run.graph_identifier,
        })
        fingerprint = checkpoint.get("fingerprint")
        if not fingerprint:
            fingerprint = self._fingerprint(run)
        checkpoint_file = checkpoint.get("checkpoint_path")
        if not checkpoint_file:
            raise DesignerError("checkpoint_package did not report a file path.")
        checkpoint_sha = sha256_file(checkpoint_file) if os.path.isfile(checkpoint_file) else None

        structure = structure_sha256(fingerprint)
        full = fingerprint_sha256(fingerprint)
        verified: Optional[bool] = None
        verify_note = None
        if verify:
            verify_result = self._call(CMD_VERIFY_CHECKPOINT, {
                "checkpoint_path": checkpoint_file,
                "graph_identifier": run.graph_identifier,
            })
            live_structure = structure_sha256(verify_result.get("fingerprint"))
            verified = (live_structure == structure)
            if not verified:
                raise DesignerError(
                    "Checkpoint verification failed: the saved copy does not "
                    "fingerprint like the working graph (expected {}, got {}). "
                    "The checkpoint file was kept at {} for inspection.".format(
                        structure, live_structure, checkpoint_file))
            verify_note = "verified via checkpoint reload"
        else:
            verify_note = "verification skipped (verify=False)"

        now = self._clock()
        revision = MaterialRevision(
            revision_id="{}:{:04d}".format(run.run_id, seq),
            run_id=run.run_id, seq=seq, created_at=now,
            parent_revision_id=run.current_revision_id,
            label=label or "r{:04d}".format(seq),
            package_index=run.package_index,
            package_path=checkpoint.get("package_path") or run.package_path,
            checkpoint_path=checkpoint_file,
            checkpoint_sha256=checkpoint_sha,
            fingerprint=fingerprint, structure_sha256=structure,
            full_sha256=full,
            output_config=self._output_config(fingerprint),
            parameters=self._parameters(fingerprint),
            notes=notes or verify_note,
            verified=verified, verified_at=now if verified is not None else None)
        self.store.insert_revision(revision)
        self.store.update_run(
            run.run_id, current_revision_id=revision.revision_id,
            baseline_revision_id=(run.baseline_revision_id or revision.revision_id),
            package_index=revision.package_index,
            package_path=revision.package_path)
        return revision

    def verify_checkpoint(self, run_id: Optional[str] = None,
                          revision_id: Optional[str] = None) -> dict:
        with self._lock:
            run = self._require_run(run_id)
            revision = self._require_revision(run, revision_id)
            if not revision.checkpoint_path:
                raise RunError("Revision '{}' has no checkpoint to verify.".format(
                    revision.revision_id))
            result = self._call(CMD_VERIFY_CHECKPOINT, {
                "checkpoint_path": revision.checkpoint_path,
                "graph_identifier": run.graph_identifier,
            })
            live_structure = structure_sha256(result.get("fingerprint"))
            verified = (live_structure == revision.structure_sha256)
            self.store.update_revision(revision.revision_id,
                                       verified=int(verified),
                                       verified_at=self._clock())
            return {
                "run_id": run.run_id,
                "revision_id": revision.revision_id,
                "checkpoint_path": revision.checkpoint_path,
                "verified": bool(verified),
                "structure_sha256": revision.structure_sha256,
                "live_structure_sha256": live_structure,
                "checkpoint_sha256": revision.checkpoint_sha256,
                "already_loaded": bool(result.get("already_loaded")),
            }

    def restore_revision(self, run_id: Optional[str] = None,
                         revision_id: Optional[str] = None,
                         save_back: bool = True,
                         force: bool = False) -> dict:
        with self._lock:
            run = self._require_run(run_id)
            if run.state in TERMINAL_STATES:
                raise RunError("Run '{}' is {}; restore is not available.".format(
                    run.run_id, run.state))
            revision = self._require_revision(run, revision_id)

            if run.current_revision_id:
                current = self.store.get_revision(run.current_revision_id)
                if current is not None:
                    drift = self._drift(run, current)
                    if drift and not force:
                        raise RunError(
                            "The working graph has un-checkpointed changes "
                            "(structure_changed={}). Restoring would discard them. "
                            "Call checkpoint_material_revision first, or pass "
                            "force=True to discard.".format(
                                drift["structure_changed"]))
            pending = self._pending_candidate(run)
            if pending and not force:
                raise RunError(
                    "Candidate '{}' is {}; decide it or pass force=True before "
                    "restoring.".format(pending.candidate_id, pending.status))

            result = self._restore_to_revision(run, revision,
                                               save_back=save_back and
                                               not _is_inside(run.package_path, self.runs_dir))
            if pending:
                self.store.update_candidate(pending.candidate_id,
                                            status=CAND_SUPERSEDED,
                                            decided_at=self._clock(),
                                            decision={"superseded_by_restore": True})
            self.store.update_run(run.run_id, current_revision_id=revision.revision_id)
            run = self.store.get_run(run.run_id)
            if run.state in (STATE_CANDIDATE_PROPOSED, STATE_AWAITING_EVALUATION,
                             STATE_EVALUATED):
                run = self._transition(run, STATE_BASELINE_READY,
                                       "restored revision {}".format(revision.revision_id))
            return {
                "run": self._public_run(run),
                "restored_to": result.get("restored_to") or revision.checkpoint_path,
                "restored_from": result.get("restored_from") or revision.checkpoint_path,
                "fingerprint_sha256": fingerprint_sha256(result.get("fingerprint")),
                "revision": self._public_revision(revision),
            }

    def propose_patch(self, run_id: Optional[str] = None,
                      operations: Optional[List[dict]] = None,
                      notes: Optional[str] = None) -> dict:
        with self._lock:
            run = self._require_run(run_id)
            if run.state != STATE_BASELINE_READY:
                raise RunError(
                    "propose_material_patch requires state '{}' (run is '{}'). "
                    "{}".format(STATE_BASELINE_READY, run.state,
                                "Finish or decide the pending candidate first."
                                if run.state in (STATE_CANDIDATE_PROPOSED,
                                                 STATE_AWAITING_EVALUATION)
                                else "Call finish_material_run to close this run."))
            if run.iteration >= run.max_iterations:
                raise RunError(
                    "Iteration budget exhausted ({} of {}). Call finish_material_run "
                    "or start a new run with a higher max_iterations.".format(
                        run.iteration, run.max_iterations))
            revision = self._require_revision(run, run.current_revision_id)

            patch = MaterialPatch.from_operations(operations, notes or "")
            validation = self._validate_patch(run, patch)
            candidate_id = "cand_" + uuid.uuid4().hex[:10]
            candidate = MaterialCandidate(
                candidate_id=candidate_id, run_id=run.run_id,
                revision_id=revision.revision_id, created_at=self._clock(),
                patch=patch.to_dict(), validation=validation,
                status=CAND_PROPOSED, apply_result=None, applied_at=None,
                decided_at=None, decision=None, error=None)
            self.store.insert_candidate(candidate)
            if not validation.get("valid"):
                self.store.update_candidate(candidate_id, status=CAND_INVALID,
                                            decided_at=self._clock())
                raise RunError("Patch is invalid: " + "; ".join(
                    e.get("message", str(e)) for e in validation.get("errors") or []))
            run = self._transition(run, STATE_CANDIDATE_PROPOSED,
                                   "candidate {} proposed".format(candidate_id))
            return {"run": self._public_run(run),
                    "candidate": self._public_candidate(
                        self.store.get_candidate(candidate_id))}

    def apply_patch(self, run_id: Optional[str] = None,
                    candidate_id: Optional[str] = None) -> dict:
        with self._lock:
            run = self._require_run(run_id)
            if run.state != STATE_CANDIDATE_PROPOSED:
                raise RunError("apply_material_patch requires state '{}' "
                               "(run is '{}').".format(STATE_CANDIDATE_PROPOSED, run.state))
            pending = self._pending_candidate(run)
            if pending is None:
                raise RunError("No pending candidate found for run '{}'.".format(run.run_id))
            if candidate_id and candidate_id != pending.candidate_id:
                raise RunError("Candidate '{}' is not the pending candidate ('{}').".format(
                    candidate_id, pending.candidate_id))
            base_revision = self._require_revision(run, pending.revision_id)

            # Re-validate against the live graph; drift between proposal and
            # application must not silently target the wrong node.
            patch = MaterialPatch.from_operations(
                (pending.patch or {}).get("operations") or [],
                (pending.patch or {}).get("notes") or "")
            validation = self._validate_patch(run, patch)
            if not validation.get("valid"):
                self.store.update_candidate(pending.candidate_id,
                                            status=CAND_INVALID,
                                            validation=validation,
                                            decided_at=self._clock(),
                                            error="re-validation failed at apply time")
                self._transition(run, STATE_BASELINE_READY,
                                 "candidate {} invalid at apply".format(pending.candidate_id))
                raise RunError("Patch failed re-validation at apply time: " + "; ".join(
                    e.get("message", str(e)) for e in validation.get("errors") or []))

            base_fingerprint = None
            if base_revision.structure_sha256:
                base_fingerprint = self._fingerprint(run)
                drift = self._drift(run, base_revision, base_fingerprint)
                if drift:
                    self.store.update_candidate(
                        pending.candidate_id, status=CAND_INVALID,
                        validation=validation, decided_at=self._clock(),
                        error="graph drifted from base revision before apply")
                    self._transition(run, STATE_BASELINE_READY,
                                     "candidate {} invalidated by drift".format(
                                         pending.candidate_id))
                    raise RunError(
                        "The graph changed since the checkpoint for revision "
                        "'{}' (structure_changed={}). Re-checkpoint and propose "
                        "again.".format(base_revision.revision_id,
                                        drift["structure_changed"]))

            apply_result, apply_error = self._apply_operations(run, patch)
            if apply_error:
                return self._fail_after_apply(run, pending, base_revision, apply_error,
                                              apply_result)

            new_fingerprint = self._call(CMD_FINGERPRINT,
                                         {"graph_identifier": run.graph_identifier})
            new_structure = structure_sha256(new_fingerprint)
            new_full = fingerprint_sha256(new_fingerprint)
            base_full = base_revision.full_sha256 or (
                fingerprint_sha256(base_fingerprint) if base_fingerprint else None)
            effective = (new_structure != base_revision.structure_sha256 or
                         new_full != base_full)
            apply_result = dict(apply_result)
            apply_result.update({
                "base_structure_sha256": base_revision.structure_sha256,
                "result_structure_sha256": new_structure,
                "result_full_sha256": new_full,
                "effective_change": bool(effective),
            })
            if not effective:
                return self._fail_after_apply(
                    run, pending, base_revision,
                    "the patch applied without producing any observable change",
                    apply_result)

            # Auto-checkpoint the applied state so previews and evaluations
            # have a verifiable revision to bind to; if that fails, the whole
            # apply counts as failed and is rolled back.
            try:
                result_revision = self._create_revision(
                    run, notes="auto-checkpoint of candidate {}".format(
                        pending.candidate_id))
            except DesignerError as exc:
                return self._fail_after_apply(
                    run, pending, base_revision,
                    "patch applied but the auto-checkpoint failed: {}".format(exc),
                    apply_result)
            apply_result.update({"result_revision_id": result_revision.revision_id})

            self.store.update_candidate(pending.candidate_id,
                                        status=CAND_APPLIED,
                                        apply_result=apply_result,
                                        applied_at=self._clock(),
                                        validation=validation)
            run = self.store.get_run(run.run_id)
            run = self._transition(run, STATE_AWAITING_EVALUATION,
                                   "candidate {} applied".format(pending.candidate_id))
            self.store.update_run(run.run_id, iteration=run.iteration + 1)
            run = self.store.get_run(run.run_id)
            return {"run": self._public_run(run),
                    "candidate": self._public_candidate(
                        self.store.get_candidate(pending.candidate_id)),
                    "apply_result": apply_result,
                    "status": "applied"}

    def _fail_after_apply(self, run: MaterialRun, candidate: MaterialCandidate,
                          base_revision: MaterialRevision, reason: str,
                          apply_result: Optional[dict]) -> dict:
        """Record an apply failure; roll the graph back to the base revision."""
        error = "Apply failed: {}".format(reason)
        rollback: Dict[str, Any] = {"attempted": True, "restored": False}
        self.store.update_candidate(candidate.candidate_id,
                                    status=CAND_APPLY_FAILED, error=error,
                                    apply_result=apply_result)
        try:
            result = self._restore_to_revision(run, base_revision)
            rollback["restored"] = True
            rollback["restored_to"] = result.get("restored_to")
        except (DesignerError, RunError) as exc:
            rollback["restored"] = False
            rollback["error"] = str(exc)

        self.store.update_run(run.run_id, current_revision_id=base_revision.revision_id)
        run = self.store.get_run(run.run_id)
        if rollback["restored"]:
            run = self._transition(run, STATE_BASELINE_READY,
                                   "rolled back after failed candidate {}".format(
                                       candidate.candidate_id))
            return {"run": self._public_run(run),
                    "candidate": self._public_candidate(
                        self.store.get_candidate(candidate.candidate_id)),
                    "status": "failed",
                    "error": error,
                    "rollback": rollback,
                    "note": "The working graph was rolled back to revision "
                            "'{}'; the run is usable.".format(base_revision.revision_id)}
        self.store.update_run(run.run_id, error=error, state=STATE_FAILED)
        run = self.store.get_run(run.run_id)
        raise RunError(
            "{}; automatic rollback ALSO failed ({}). The run is marked 'failed' "
            "and the graph may be in an inconsistent state. Checkpoint/restore "
            "manually.".format(error, rollback.get("error")))

    def _apply_operations(self, run: MaterialRun,
                          patch: MaterialPatch) -> tuple:
        results: List[dict] = []
        size_ops = [op for op in patch.operations if op.op == "set_output_size"]
        param_ops = [op for op in patch.operations if op.op == "set_parameter"]
        try:
            for op in size_ops:
                result = self._call(CMD_SET_OUTPUT_SIZE, {
                    "graph_identifier": run.graph_identifier,
                    "width_log2": int(op.width_log2),
                    "height_log2": int(op.height_log2),
                })
                results.append({"op": op.to_dict(), "result": result})
            if param_ops:
                updates = []
                for op in param_ops:
                    entry = {"node_id": op.node_id,
                             "parameter_id": op.parameter_id,
                             "value": op.value}
                    if op.value_type:
                        entry["value_type"] = op.value_type
                    updates.append(entry)
                result = self._call(CMD_SET_PARAMETERS, {
                    "graph_identifier": run.graph_identifier,
                    "updates": updates})
                failed = [r for r in (result.get("results") or [])
                          if r.get("status") == "error"]
                if result.get("failed") or failed:
                    detail = "; ".join(
                        "{}: {}".format(r.get("parameter_id"), r.get("error"))
                        for r in failed[:5]) or "{} update(s) failed".format(
                        result.get("failed"))
                    return ({"steps": results, "set_parameters": result},
                            "set_parameters reported failures ({})".format(detail))
                results.append({"op": "set_parameters", "result": result})
        except DesignerError as exc:
            return ({"steps": results}, str(exc))
        return ({"steps": results}, None)

    def capture_preview(self, run_id: Optional[str] = None,
                        output_usages: Optional[List[str]] = None,
                        max_size: Optional[int] = None,
                        histogram_bins: Optional[int] = None,
                        include_3d: Optional[bool] = None,
                        wait_ms: Optional[int] = None,
                        refresh_first: Optional[bool] = None) -> dict:
        with self._lock:
            run = self._require_run(run_id)
            if run.state in TERMINAL_STATES:
                raise RunError("Run '{}' is {}; no more previews.".format(
                    run.run_id, run.state))
            revision = self._require_revision(run, run.current_revision_id)
            preset = run.preview_preset or {}
            fingerprint = self._assert_no_drift(run, revision)
            full_sha = fingerprint_sha256(fingerprint)

            usages = output_usages or preset.get("output_usages")
            max_size = int(max_size if max_size is not None else preset.get("max_size", 1024))
            histogram_bins = int(histogram_bins if histogram_bins is not None
                                 else preset.get("histogram_bins", 16))
            include_3d = bool(include_3d if include_3d is not None
                              else preset.get("include_3d", True))
            wait_ms = int(wait_ms if wait_ms is not None else preset.get("wait_ms", 600))
            refresh_first = bool(refresh_first if refresh_first is not None
                                 else preset.get("refresh_first", True))

            rev_dir = os.path.join(self._run_dir(run.run_id), "revisions",
                                   "{:04d}".format(revision.seq))
            os.makedirs(rev_dir, exist_ok=True)
            stamp = time.strftime("%Y%m%d-%H%M%S")
            sheet_path = os.path.join(rev_dir, "{}_outputs.png".format(stamp))

            sheet = self._call(CMD_INSPECT_OUTPUTS, {
                "graph_identifier": run.graph_identifier,
                "output_usages": usages,
                "max_size": max_size,
                "histogram_bins": histogram_bins,
                "output_path": sheet_path,
            })
            stats = self._trim_stats(sheet)
            skipped = sheet.get("skipped") or []
            sheet_file = sheet.get("path") or sheet_path
            sheet_cfg = {"output_usages": usages, "max_size": max_size,
                         "histogram_bins": histogram_bins, "skipped": skipped}
            checks = self._technical_checks(run, stats, skipped, usages)
            sheet_sha = None
            sheet_ok = bool(os.path.isfile(sheet_file))
            if sheet_ok:
                sheet_sha = sha256_file(sheet_file)
            sheet_id = self.store.insert_artifact(
                run_id=run.run_id, revision_id=revision.revision_id,
                kind="outputs_sheet", path=sheet_file if sheet_ok else None,
                sha256=sheet_sha, config=sheet_cfg, stats=stats,
                source_fingerprint=full_sha, status="ok" if sheet_ok else "missing",
                error=None if sheet_ok else "contact sheet file not found")

            artifacts = [{"artifact_id": sheet_id, "kind": "outputs_sheet",
                          "path": sheet_file, "sha256": sheet_sha,
                          "width": sheet.get("width"), "height": sheet.get("height"),
                          "stats": stats, "skipped": skipped}]
            warnings: List[str] = []
            render = None
            if include_3d:
                render, send_error = self._capture_render(
                    run, revision, rev_dir, stamp, wait_ms, refresh_first, full_sha)
                if render is None:
                    warnings.append("3D preview unavailable: {}".format(send_error))
                else:
                    artifacts.append(render)

            technical_passed = all(c.get("passed") for c in checks)
            return {
                "run_id": run.run_id,
                "revision_id": revision.revision_id,
                "captured_at": self._clock(),
                "source_fingerprint": full_sha,
                "artifacts": artifacts,
                "technical_checks": checks,
                "technical_passed": technical_passed,
                "skipped_outputs": skipped,
                "warnings": warnings,
                "paths": [a.get("path") for a in artifacts if a.get("path")],
            }

    def _capture_render(self, run: MaterialRun, revision: MaterialRevision,
                        rev_dir: str, stamp: str, wait_ms: int,
                        refresh_first: bool, source_fingerprint: Optional[str]) -> tuple:
        """Bind the graph in the 3D view and capture it. Returns
        (artifact_dict, None) or (None, error_message). The 3D view must be
        open in SD; this is a best-effort part of a preview."""
        timeout_s = float((run.preview_preset or {}).get("send_timeout_s", 90.0))
        send, send_error = self._call_optional(
            CMD_SEND_3D, {"graph_identifier": run.graph_identifier})
        if send_error:
            return None, send_error
        job_id = (send or {}).get("job_id")
        if job_id is None:
            return None, "send_to_3d_view returned no job id"
        status = send
        deadline = time.monotonic() + max(5.0, timeout_s)
        while not (status or {}).get("done"):
            if time.monotonic() >= deadline:
                return None, "send_to_3d_view did not finish within {:.0f}s".format(
                    timeout_s)
            time.sleep(0.5)
            status, error = self._call_optional(
                CMD_SEND_3D_STATUS, {"job_id": job_id})
            if error:
                return None, error
        failed = (status or {}).get("failed") or []
        pending = (status or {}).get("pending") or []
        if pending or failed:
            return None, "send_to_3d_view incomplete (failed={}, pending={})".format(
                failed, pending)

        render_path = os.path.join(rev_dir, "{}_3d.png".format(stamp))
        shot = self._call(CMD_CAPTURE_3D, {
            "output_path": render_path,
            "refresh_first": bool(refresh_first),
            "wait_ms": wait_ms,
        })
        path = shot.get("path") or render_path
        if not path or not os.path.isfile(path):
            return None, "capture_3d_view produced no file"
        artifact = {"kind": "render_3d", "path": path,
                    "sha256": sha256_file(path),
                    "width": shot.get("width"), "height": shot.get("height")}
        artifact_id = self.store.insert_artifact(
            run_id=run.run_id, revision_id=revision.revision_id,
            kind="render_3d", path=path, sha256=artifact["sha256"],
            config={"wait_ms": wait_ms, "refresh_first": refresh_first},
            stats=None, source_fingerprint=source_fingerprint,
            status="ok", error=None)
        artifact["artifact_id"] = artifact_id
        return artifact, None

    def record_evaluation(self, run_id: Optional[str] = None,
                          verdict: Optional[str] = None,
                          score: Optional[float] = None,
                          accepted: Optional[bool] = None,
                          evaluator: Optional[str] = None,
                          summary: Optional[str] = None,
                          criteria: Optional[list] = None,
                          regression_threshold: Optional[float] = None) -> dict:
        with self._lock:
            run = self._require_run(run_id)
            if run.state != STATE_AWAITING_EVALUATION:
                raise RunError(
                    "record_material_evaluation requires state '{}' (run is "
                    "'{}'). Apply a patch first.".format(
                        STATE_AWAITING_EVALUATION, run.state))
            candidate = self._pending_candidate(run)
            if candidate is None or candidate.status != CAND_APPLIED:
                raise RunError("No applied candidate is awaiting evaluation.")
            # The candidate's *base* revision is where the patch started; the
            # applied state was auto-checkpointed as the current revision.
            applied_id = (candidate.apply_result or {}).get("result_revision_id")
            revision = self._require_revision(run, applied_id or run.current_revision_id)
            base_revision = self._require_revision(run, candidate.revision_id)

            artifact = self.store.latest_artifact(
                run.run_id, kind="outputs_sheet", revision_id=revision.revision_id)
            if artifact is None or artifact.get("status") != "ok":
                raise RunError(
                    "No preview artifact for revision '{}'. Call "
                    "capture_material_preview first; the graph must not change "
                    "between capture and evaluation.".format(revision.revision_id))
            if candidate.applied_at and artifact.get("created_at", 0) < candidate.applied_at:
                raise RunError(
                    "The preview artifact for revision '{}' predates the patch "
                    "application (stale). Capture a fresh preview with "
                    "capture_material_preview.".format(revision.revision_id))
            if artifact.get("source_fingerprint") != revision.full_sha256:
                raise RunError(
                    "The preview artifact does not match revision '{}' "
                    "(source fingerprint {} != {}). The graph changed after the "
                    "capture; capture a fresh preview.".format(
                        revision.revision_id, artifact.get("source_fingerprint"),
                        revision.full_sha256))

            parent_id = base_revision.revision_id
            before_stats = None
            before_artifact = self.store.latest_artifact(
                run.run_id, kind="outputs_sheet", revision_id=parent_id)
            if before_artifact and before_artifact.get("status") == "ok":
                before_stats = before_artifact.get("stats")
            warnings: List[str] = []
            if before_stats is None:
                warnings.append("No baseline preview stats were recorded for revision "
                                "'{}'; regression comparison was skipped. Capture a "
                                "preview for the base revision before applying "
                                "patches to enable comparisons.".format(parent_id))

            after_stats = artifact.get("stats") or {}
            threshold = float(regression_threshold if regression_threshold is not None
                              else DEFAULT_REGRESSION_THRESHOLD)
            comparisons = self._compare_stats(before_stats, after_stats, threshold)
            artifact_cfg = artifact.get("config") or {}
            checks = self._technical_checks(
                run, after_stats, artifact_cfg.get("skipped") or [],
                artifact_cfg.get("output_usages") or
                (run.preview_preset or {}).get("output_usages") or [])
            technical_passed = all(c.get("passed") for c in checks)

            if accepted is None:
                accepted = technical_passed and not comparisons.get("threshold_failures")
                if before_stats is None:
                    accepted = technical_passed
            accepted = bool(accepted)

            evaluation = MaterialEvaluation(
                evaluation_id="eval_" + uuid.uuid4().hex[:10],
                run_id=run.run_id, candidate_id=candidate.candidate_id,
                revision_id=revision.revision_id, created_at=self._clock(),
                evaluator=evaluator or "external",
                summary=summary or "", verdict=verdict or "recorded",
                accepted=accepted, score=score, criteria=criteria or [],
                regression_threshold=threshold,
                baseline_revision_id=revision.parent_revision_id,
                comparisons=comparisons, technical_checks=checks,
                technical_passed=technical_passed, warnings=warnings,
                artifact_id=artifact.get("artifact_id"))
            self.store.insert_evaluation(evaluation)
            self.store.update_candidate(candidate.candidate_id,
                                        status=CAND_EVALUATED)
            run = self._transition(run, STATE_EVALUATED,
                                   "candidate {} evaluated".format(candidate.candidate_id))
            return {"run": self._public_run(run),
                    "evaluation": self._public_evaluation(evaluation),
                    "candidate": self._public_candidate(
                        self.store.get_candidate(candidate.candidate_id))}

    def decide_candidate(self, run_id: Optional[str] = None,
                         accepted: Optional[bool] = None,
                         restore: bool = True,
                         notes: Optional[str] = None) -> dict:
        with self._lock:
            run = self._require_run(run_id)
            pending = self._pending_candidate(run)
            if pending is None:
                raise RunError("No pending candidate to decide in run '{}'.".format(
                    run.run_id))
            if run.state == STATE_EVALUATED:
                evaluation = self.store.latest_evaluation(pending.candidate_id)
                if evaluation is None:
                    raise RunError("No evaluation found for candidate '{}'.".format(
                        pending.candidate_id))
                if accepted is None:
                    accepted = evaluation.accepted
            elif run.state == STATE_CANDIDATE_PROPOSED:
                if accepted is True:
                    raise RunError("Candidate '{}' has not been applied/evaluated "
                                   "yet; apply and evaluate before accepting.".format(
                                       pending.candidate_id))
                accepted = False
            else:
                raise RunError(
                    "decide_material_candidate requires state '{}' or '{}' (run is "
                    "'{}').".format(STATE_EVALUATED, STATE_CANDIDATE_PROPOSED, run.state))
            accepted = bool(accepted)
            base_revision = self._require_revision(run, pending.revision_id)

            decision = {"accepted": accepted, "notes": notes or "",
                        "at": self._clock()}
            if accepted:
                applied_revision_id = (pending.apply_result or {}).get(
                    "result_revision_id") or run.current_revision_id
                self.store.update_candidate(pending.candidate_id,
                                            status=CAND_ACCEPTED,
                                            decided_at=decision["at"],
                                            decision=decision)
                self.store.update_run(run.run_id,
                                      best_revision_id=applied_revision_id,
                                      current_revision_id=applied_revision_id)
                run = self.store.get_run(run.run_id)
                reason = "candidate {} accepted".format(pending.candidate_id)
                if run.iteration >= run.max_iterations:
                    run = self._finalize_run(
                        run, reason="iteration budget exhausted ({} of {})".format(
                            run.iteration, run.max_iterations),
                        restore_best=False, notes=notes)
                else:
                    run = self._transition(run, STATE_BASELINE_READY, reason)
                return {"run": self._public_run(run),
                        "candidate": self._public_candidate(
                            self.store.get_candidate(pending.candidate_id)),
                        "decision": "accepted"}

            self.store.update_candidate(pending.candidate_id, status=CAND_REJECTED,
                                        decided_at=decision["at"], decision=decision)
            was_applied = pending.status in (CAND_APPLIED, CAND_EVALUATED) and bool(
                (pending.apply_result or {}).get("result_revision_id"))
            rollback: Dict[str, Any] = {"attempted": bool(restore and was_applied),
                                        "restored": False}
            if restore and not was_applied:
                rollback["reason"] = ("candidate was never applied; nothing to "
                                      "roll back")
            if restore and was_applied:
                try:
                    result = self._restore_to_revision(run, base_revision)
                    rollback["restored"] = True
                    rollback["restored_to"] = result.get("restored_to")
                except (DesignerError, RunError) as exc:
                    rollback["error"] = str(exc)
                    self.store.update_run(
                        run.run_id, state=STATE_FAILED, error=str(exc),
                        current_revision_id=base_revision.revision_id)
                    raise RunError(
                        "Candidate rejected, but restoring revision '{}' failed: {}. "
                        "The run is marked 'failed'.".format(
                            base_revision.revision_id, exc))
            self.store.update_run(run.run_id, current_revision_id=base_revision.revision_id)
            run = self.store.get_run(run.run_id)
            run = self._transition(run, STATE_BASELINE_READY,
                                   "candidate {} rejected".format(pending.candidate_id))
            return {"run": self._public_run(run),
                    "candidate": self._public_candidate(
                        self.store.get_candidate(pending.candidate_id)),
                    "decision": "rejected",
                    "rollback": rollback}

    def finish_run(self, run_id: Optional[str] = None, notes: Optional[str] = None,
                   restore_best: bool = True) -> dict:
        with self._lock:
            run = self._require_run(run_id)
            if run.state in TERMINAL_STATES:
                raise RunError("Run '{}' is already {}.".format(run.run_id, run.state))
            if run.state not in (STATE_CREATED, STATE_BASELINE_READY):
                raise RunError(
                    "finish_material_run requires state '{}' or '{}' (run is "
                    "'{}'). Decide the pending candidate first.".format(
                        STATE_CREATED, STATE_BASELINE_READY, run.state))
            run = self._finalize_run(run, reason="finished by request",
                                     restore_best=restore_best, notes=notes)
            return {"run": self._public_run(run), "report": run.report}

    def _finalize_run(self, run: MaterialRun, reason: str, restore_best: bool,
                      notes: Optional[str]) -> MaterialRun:
        restored_to = None
        restore_error = None
        best = self.store.get_revision(run.best_revision_id) \
            if run.best_revision_id else None
        if restore_best and best is not None:
            needs_restore = run.current_revision_id != best.revision_id
            if not needs_restore and best.structure_sha256:
                try:
                    needs_restore = self._drift(run, best) is not None
                except DesignerError:
                    needs_restore = False
            if needs_restore:
                try:
                    result = self._restore_to_revision(run, best)
                    restored_to = result.get("restored_to")
                except (DesignerError, RunError) as exc:
                    restore_error = str(exc)
                self.store.update_run(run.run_id,
                                      current_revision_id=best.revision_id)
        report = self._build_report(run, reason, restored_to, restore_error, notes)
        self.store.update_run(run.run_id, state=STATE_FINISHED, report=report,
                              finished_at=self._clock(), error=restore_error)
        return self.store.get_run(run.run_id)

    # ══════════════════════════════════════════════════════════════════
    # Validation / metrics
    # ══════════════════════════════════════════════════════════════════

    def _validate_patch(self, run: MaterialRun,
                        patch: MaterialPatch) -> dict:
        errors: List[dict] = []
        warnings: List[dict] = []

        def error(code: str, message: str, **extra: Any) -> None:
            entry = {"code": code, "message": message}
            entry.update(extra)
            errors.append(entry)

        def warn(code: str, message: str, **extra: Any) -> None:
            entry = {"code": code, "message": message}
            entry.update(extra)
            warnings.append(entry)

        operations = patch.operations
        if not operations:
            error("empty_patch", "The patch has no operations.")
        if len(operations) > PATCH_MAX_OPERATIONS:
            error("too_many_operations",
                  "Patch has {} operations; the limit is {}.".format(
                      len(operations), PATCH_MAX_OPERATIONS))

        size_ops = 0
        seen: set = set()
        node_props: Dict[str, Optional[dict]] = {}
        for index, op in enumerate(operations):
            if op.op not in ALLOWED_PATCH_OPS:
                error("unknown_op",
                      "Operation {} has unknown op '{}'. Allowed: {}.".format(
                          index, op.op, ", ".join(sorted(ALLOWED_PATCH_OPS))))
                continue
            if op.op == "set_output_size":
                size_ops += 1
                if size_ops > 1:
                    error("duplicate_output_size",
                          "Only one set_output_size operation is allowed.")
                for name, value in (("width_log2", op.width_log2),
                                    ("height_log2", op.height_log2)):
                    if value is None:
                        error("missing_field",
                              "set_output_size requires '{}'.".format(name))
                    elif not isinstance(value, int) or isinstance(value, bool):
                        error("bad_field", "'{}' must be an integer log2.".format(name))
                    elif not (OUTPUT_SIZE_LOG2_RANGE[0] <= value <= OUTPUT_SIZE_LOG2_RANGE[1]):
                        error("bad_field",
                              "'{}'={} is outside {}..{}.".format(
                                  name, value, *OUTPUT_SIZE_LOG2_RANGE))
                continue

            if not op.node_id or not op.parameter_id:
                error("missing_field",
                      "set_parameter requires 'node_id' and 'parameter_id' "
                      "(operation {}).".format(index))
                continue
            if op.value is _UNSET:
                error("missing_field",
                      "set_parameter for '{}.{}' requires 'value'.".format(
                          op.node_id, op.parameter_id))
                continue
            value_type = (op.value_type or "float").lower()
            if value_type not in ALLOWED_VALUE_TYPES:
                error("bad_value_type",
                      "Unknown value_type '{}'. Allowed: {}.".format(
                          op.value_type, ", ".join(sorted(ALLOWED_VALUE_TYPES))))
                continue
            shape_error = self._value_shape_error(value_type, op.value)
            if shape_error:
                error("bad_value", "Invalid value for '{}.{}': {}".format(
                    op.node_id, op.parameter_id, shape_error))
                continue
            key = (op.node_id, op.parameter_id)
            if key in seen:
                error("duplicate_parameter",
                      "Parameter '{}.{}' appears more than once.".format(*key))
                continue
            seen.add(key)

            if op.node_id not in node_props:
                info, call_error = self._call_optional(
                    CMD_GET_NODE_INFO,
                    {"node_id": op.node_id, "graph_identifier": run.graph_identifier})
                node_props[op.node_id] = info if info else None
                if call_error:
                    error("node_lookup_failed",
                          "Node '{}' could not be inspected: {}".format(
                              op.node_id, call_error),
                          node_id=op.node_id)
            info = node_props[op.node_id]
            if info is None:
                continue
            valid_ids = {p.get("id") for p in info.get("inputs") or []}
            valid_ids |= {p.get("id") for p in info.get("annotations") or []}
            if op.parameter_id not in valid_ids:
                suggestion = difflib.get_close_matches(
                    op.parameter_id, sorted(valid_ids), n=3, cutoff=0.4)
                error("unknown_parameter",
                      "Node '{}' has no parameter '{}'{}.".format(
                          op.node_id, op.parameter_id,
                          "; did you mean {}?".format(", ".join(suggestion))
                          if suggestion else ""),
                      node_id=op.node_id, parameter_id=op.parameter_id,
                      available=sorted(valid_ids))
                continue
            for prop in info.get("inputs") or []:
                if prop.get("id") == op.parameter_id and prop.get("connected_from"):
                    warn("input_connected",
                         "Input '{}.{}' is connected ({}); setting a value may have "
                         "no effect until the connection is removed.".format(
                             op.node_id, op.parameter_id,
                             ", ".join(prop["connected_from"])))

        validation = {
            "valid": not errors,
            "errors": errors,
            "warnings": warnings,
            "checked_operations": len(operations),
            "validated_at": self._clock(),
            "graph_identifier": run.graph_identifier,
        }
        return validation

    @staticmethod
    def _value_shape_error(value_type: str, value: Any) -> Optional[str]:
        if value_type in ("float", "int"):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return "expected a number, got {!r}".format(value)
            return None
        if value_type == "bool":
            return None if isinstance(value, bool) else "expected true/false, got {!r}".format(value)
        if value_type == "string":
            return None if isinstance(value, str) else "expected a string, got {!r}".format(value)
        lengths = {"float2": 2, "int2": 2, "float3": 3, "int3": 3,
                   "float4": 4, "int4": 4, "color": 3}
        length = lengths[value_type]
        if not isinstance(value, (list, tuple)) or \
                not (length <= len(value) <= (4 if value_type == "color" else length)):
            return "expected a list of {} ({} for color), got {!r}".format(
                length, length + 1, value)
        for item in value:
            if isinstance(item, bool) or not isinstance(item, (int, float)):
                return "expected numbers, got {!r}".format(value)
        return None

    def _validate_constraints(self, constraints: list) -> tuple:
        checks: List[dict] = []
        errors: List[str] = []
        for index, item in enumerate(constraints):
            if not isinstance(item, dict):
                errors.append("constraint {} is not an object".format(index))
                continue
            output = item.get("output") or item.get("usage")
            channel = item.get("channel")
            metric = item.get("metric", "mean")
            minimum, maximum = item.get("min"), item.get("max")
            if not output:
                errors.append("constraint {} needs 'output'".format(index))
                continue
            if metric not in _METRICS:
                errors.append("constraint {} has metric '{}' (allowed: {})".format(
                    index, metric, ", ".join(_METRICS)))
                continue
            if minimum is None and maximum is None:
                errors.append("constraint {} needs 'min' and/or 'max'".format(index))
                continue
            checks.append({
                "name": item.get("name") or "{}:{}.{}.{}".format(
                    output, metric, channel or "any",
                    "range[{},{}]".format(minimum, maximum)),
                "output": output, "channel": channel, "metric": metric,
                "min": minimum, "max": maximum,
            })
        return checks, errors

    @staticmethod
    def _trim_stats(payload: dict) -> dict:
        trimmed: Dict[str, Any] = {}
        for entry in payload.get("outputs") or []:
            usage = entry.get("usage") or entry.get("label") or entry.get("node")
            if not usage:
                continue
            channels = {}
            for channel, stats in (entry.get("channels") or {}).items():
                channels[channel] = {
                    key: stats.get(key)
                    for key in ("min", "max", "mean", "count", "non_finite")
                }
            trimmed[str(usage)] = {
                "channels": channels,
                "width": entry.get("width"),
                "height": entry.get("height"),
                "pixel_format": entry.get("pixel_format"),
                "node": entry.get("node"),
                "label": entry.get("label"),
            }
        return trimmed

    def _technical_checks(self, run: MaterialRun,
                          stats: dict, skipped: list,
                          expected_usages: Optional[List[str]]) -> List[dict]:
        checks: List[dict] = []
        skipped_norm = {_norm_usage(s if isinstance(s, str)
                                    else (s.get("usage") or s.get("label") or ""))
                        for s in skipped or []}
        stats_norm = {_norm_usage(k): k for k in stats}
        for usage in expected_usages or []:
            key = _norm_usage(usage)
            present = key in stats_norm
            detail = "captured" if present else (
                "listed in skipped outputs" if key in skipped_norm
                else "not found in the graph outputs")
            checks.append({"name": "output_present:{}".format(usage),
                           "kind": "presence", "output": usage,
                           "passed": bool(present), "detail": detail})
        for usage, info in stats.items():
            non_finite = sum((c.get("non_finite") or 0)
                             for c in (info.get("channels") or {}).values())
            checks.append({"name": "finite:{}".format(usage), "kind": "finite",
                           "output": usage, "passed": non_finite == 0,
                           "detail": {"non_finite_pixels": non_finite}})
        for constraint in run.constraints or []:
            value = self._lookup_metric(stats, constraint.get("output"),
                                        constraint.get("channel"),
                                        constraint.get("metric", "mean"))
            passed = value is not None
            if passed and constraint.get("min") is not None:
                passed = value >= float(constraint["min"])
            if passed and constraint.get("max") is not None:
                passed = value <= float(constraint["max"])
            checks.append({
                "name": "constraint:{}".format(constraint.get("name")),
                "kind": "constraint", "output": constraint.get("output"),
                "passed": bool(passed),
                "detail": {"metric": constraint.get("metric"),
                           "channel": constraint.get("channel"),
                           "value": value, "min": constraint.get("min"),
                           "max": constraint.get("max")},
            })
        return checks

    @staticmethod
    def _lookup_metric(stats: Optional[dict], output: Optional[str],
                       channel: Optional[str], metric: str) -> Optional[float]:
        if not stats or not output:
            return None
        target = None
        for key, info in stats.items():
            if _norm_usage(key) == _norm_usage(str(output)):
                target = info
                break
        if target is None:
            return None
        channels = target.get("channels") or {}
        if channel and channel in channels:
            return channels[channel].get(metric)
        for candidate in ("L", "R", "Y", "1"):
            if candidate in channels:
                return channels[candidate].get(metric)
        if channels:
            first = next(iter(channels.values()))
            return first.get(metric)
        return None

    def _compare_stats(self, before: Optional[dict], after: dict,
                       threshold: float) -> dict:
        comparisons: Dict[str, Any] = {
            "threshold": threshold, "regressions": [], "improvements": [],
            "threshold_failures": [], "skipped": False,
        }
        if not before:
            comparisons["skipped"] = True
            return comparisons
        for usage, info in (after or {}).items():
            before_info = None
            for key, value in (before or {}).items():
                if _norm_usage(key) == _norm_usage(usage):
                    before_info = value
                    break
            if before_info is None:
                continue
            for channel, stats in (info.get("channels") or {}).items():
                b = (before_info.get("channels") or {}).get(channel)
                if not b:
                    continue
                for metric in ("mean", "min", "max"):
                    bval, aval = b.get(metric), stats.get(metric)
                    if bval is None or aval is None:
                        continue
                    denom = max(abs(float(bval)), 1e-6)
                    relative = (float(aval) - float(bval)) / denom
                    entry = {"output": usage, "channel": channel, "metric": metric,
                             "before": bval, "after": aval,
                             "relative": round(relative, 6)}
                    if relative < -threshold:
                        comparisons["regressions"].append(entry)
                        if metric == "mean":
                            comparisons["threshold_failures"].append(entry)
                    elif relative > threshold:
                        comparisons["improvements"].append(entry)
        return comparisons

    # ══════════════════════════════════════════════════════════════════
    # Fingerprint-derived metadata
    # ══════════════════════════════════════════════════════════════════

    @staticmethod
    def _output_config(fingerprint: dict) -> dict:
        config: Dict[str, Any] = {"graph_inputs": {}}
        for prop in fingerprint.get("graph_inputs") or []:
            pid = prop.get("id")
            value = prop.get("value")
            if pid:
                config["graph_inputs"][pid] = value
            if pid != "$outputsize":
                continue
            if isinstance(value, dict) and "x" in value and "y" in value:
                config["width_log2"], config["height_log2"] = value["x"], value["y"]
            elif isinstance(value, (list, tuple)) and len(value) >= 2:
                config["width_log2"], config["height_log2"] = value[0], value[1]
        return config

    @staticmethod
    def _parameters(fingerprint: dict) -> dict:
        parameters: Dict[str, Any] = {}
        for node in fingerprint.get("nodes") or []:
            values: Dict[str, Any] = {}
            for prop in (node.get("inputs") or []) + (node.get("annotations") or []):
                if prop.get("id") and prop.get("value") is not None and \
                        not prop.get("connected"):
                    values[prop["id"]] = prop["value"]
            if values:
                parameters[node.get("id")] = values
        return parameters

    # ══════════════════════════════════════════════════════════════════
    # Serialisation
    # ══════════════════════════════════════════════════════════════════

    def _run_bundle(self, run: MaterialRun, include_artifacts: bool) -> dict:
        pending = self._pending_candidate(run)
        bundle = {
            "run": self._public_run(run),
            "revisions": [self._public_revision(r) for r in
                          self.store.list_revisions(run.run_id)],
            "candidates": [self._public_candidate(c) for c in
                           self.store.list_candidates(run.run_id)],
            "evaluations": [self._public_evaluation(e) for e in
                            self.store.list_evaluations(run.run_id)],
            "pending_candidate": self._public_candidate(pending) if pending else None,
        }
        if include_artifacts:
            bundle["artifacts"] = self.store.list_artifacts(run.run_id)
        return bundle

    @staticmethod
    def _public_run(run: MaterialRun, brief: bool = False) -> dict:
        data = {
            "run_id": run.run_id,
            "state": run.state,
            "graph_identifier": run.graph_identifier,
            "goal": run.goal,
            "iteration": run.iteration,
            "max_iterations": run.max_iterations,
            "baseline_revision_id": run.baseline_revision_id,
            "current_revision_id": run.current_revision_id,
            "best_revision_id": run.best_revision_id,
            "package_index": run.package_index,
            "package_path": run.package_path,
            "created_at": run.created_at,
            "updated_at": run.updated_at,
            "finished_at": run.finished_at,
            "error": run.error,
        }
        if not brief:
            data.update({
                "preview_preset": run.preview_preset,
                "constraints": run.constraints,
                "seeds": run.seeds,
                "transitions": run.transitions,
                "report": run.report,
            })
        return data

    @staticmethod
    def _public_revision(revision: MaterialRevision) -> dict:
        return {
            "revision_id": revision.revision_id,
            "seq": revision.seq,
            "label": revision.label,
            "parent_revision_id": revision.parent_revision_id,
            "created_at": revision.created_at,
            "package_index": revision.package_index,
            "package_path": revision.package_path,
            "checkpoint_path": revision.checkpoint_path,
            "checkpoint_sha256": revision.checkpoint_sha256,
            "structure_sha256": revision.structure_sha256,
            "full_sha256": revision.full_sha256,
            "output_config": revision.output_config,
            "notes": revision.notes,
            "verified": revision.verified,
            "verified_at": revision.verified_at,
        }

    @staticmethod
    def _public_candidate(candidate: Optional[MaterialCandidate]) -> Optional[dict]:
        if candidate is None:
            return None
        return {
            "candidate_id": candidate.candidate_id,
            "revision_id": candidate.revision_id,
            "base_revision_id": candidate.revision_id,
            "created_at": candidate.created_at,
            "patch": candidate.patch,
            "validation": candidate.validation,
            "status": candidate.status,
            "apply_result": candidate.apply_result,
            "applied_at": candidate.applied_at,
            "decided_at": candidate.decided_at,
            "decision": candidate.decision,
            "error": candidate.error,
        }

    @staticmethod
    def _public_evaluation(evaluation: MaterialEvaluation) -> dict:
        return {
            "evaluation_id": evaluation.evaluation_id,
            "candidate_id": evaluation.candidate_id,
            "revision_id": evaluation.revision_id,
            "created_at": evaluation.created_at,
            "evaluator": evaluation.evaluator,
            "summary": evaluation.summary,
            "verdict": evaluation.verdict,
            "accepted": evaluation.accepted,
            "score": evaluation.score,
            "criteria": evaluation.criteria,
            "regression_threshold": evaluation.regression_threshold,
            "baseline_revision_id": evaluation.baseline_revision_id,
            "comparisons": evaluation.comparisons,
            "technical_checks": evaluation.technical_checks,
            "technical_passed": evaluation.technical_passed,
            "warnings": evaluation.warnings,
            "artifact_id": evaluation.artifact_id,
        }

    def _build_report(self, run: MaterialRun, reason: str,
                      restored_to: Optional[str], restore_error: Optional[str],
                      notes: Optional[str]) -> dict:
        revisions = self.store.list_revisions(run.run_id)
        candidates = self.store.list_candidates(run.run_id)
        evaluations = self.store.list_evaluations(run.run_id)
        artifacts = self.store.list_artifacts(run.run_id)
        by_revision: Dict[str, List[dict]] = {}
        for artifact in artifacts:
            by_revision.setdefault(artifact.get("revision_id"), []).append({
                "kind": artifact.get("kind"), "path": artifact.get("path"),
                "sha256": artifact.get("sha256"),
                "created_at": artifact.get("created_at"),
                "status": artifact.get("status"),
            })
        return {
            "run_id": run.run_id,
            "graph_identifier": run.graph_identifier,
            "goal": run.goal,
            "reason": reason,
            "state": STATE_FINISHED,
            "iterations_used": run.iteration,
            "max_iterations": run.max_iterations,
            "baseline_revision_id": run.baseline_revision_id,
            "current_revision_id": run.current_revision_id,
            "best_revision_id": run.best_revision_id,
            "restored_to_revision_id": run.best_revision_id if restored_to else None,
            "restored_to_path": restored_to,
            "restore_error": restore_error,
            "revisions": [{
                "revision_id": r.revision_id, "seq": r.seq, "label": r.label,
                "parent_revision_id": r.parent_revision_id,
                "checkpoint_path": r.checkpoint_path,
                "checkpoint_sha256": r.checkpoint_sha256,
                "structure_sha256": r.structure_sha256,
                "full_sha256": r.full_sha256,
                "verified": r.verified,
                "created_at": r.created_at,
            } for r in revisions],
            "candidates": [{
                "candidate_id": c.candidate_id, "revision_id": c.revision_id,
                "status": c.status,
                "operations": ((c.patch or {}).get("operations") or []),
                "decision": c.decision, "error": c.error,
            } for c in candidates],
            "evaluations": [{
                "evaluation_id": e.evaluation_id, "candidate_id": e.candidate_id,
                "verdict": e.verdict, "accepted": e.accepted, "score": e.score,
                "technical_passed": e.technical_passed,
                "regression_threshold": e.regression_threshold,
                "comparisons": e.comparisons,
                "summary": e.summary,
            } for e in evaluations],
            "artifacts": by_revision,
            "reproducibility": {
                "store": self.store.db_path,
                "run_dir": self._run_dir(run.run_id),
                "preview_preset": run.preview_preset,
                "constraints": run.constraints,
                "seeds": run.seeds,
                "notes": notes or "",
                "transition_log": run.transitions,
            },
            "finished_at": self._clock(),
        }
