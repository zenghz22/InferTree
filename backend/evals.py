from __future__ import annotations

import asyncio
import json
import re
import statistics
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from fastapi import HTTPException

from .agent import run_agent
from .config import AgentConfig, REASONING_POLICIES, resolve_model


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SUITES_ROOT = PROJECT_ROOT / "evals" / "suites"
RUNS_ROOT = PROJECT_ROOT / "data" / "evals" / "runs"
SUITE_ID_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
RUN_ID_RE = re.compile(r"eval_[0-9a-f]{32}")
ALLOWED_MODES = {"fixture", "agent"}
ALLOWED_GRADERS = {
    "no_error",
    "output_nonempty",
    "contains",
    "not_contains",
    "starts_with",
    "regex",
    "tool_called",
    "artifact_count",
    "latency_ms",
}
MAX_CASES = 50
MAX_SAMPLES = 100
MAX_REPEATS = 3
MAX_STORED_OUTPUT_CHARS = 30_000
MAX_STORED_ACTIVITIES = 120
_LOCK = threading.RLock()
_ACTIVE: Dict[str, Tuple[threading.Thread, threading.Event]] = {}


class _EvalCancelled(Exception):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _suite_path(suite_id: str) -> Path:
    if not isinstance(suite_id, str) or not SUITE_ID_RE.fullmatch(suite_id):
        raise HTTPException(status_code=422, detail="Invalid evaluation suite_id.")
    return SUITES_ROOT / f"{suite_id}.json"


def _run_path(run_id: str) -> Path:
    if not isinstance(run_id, str) or not RUN_ID_RE.fullmatch(run_id):
        raise HTTPException(status_code=422, detail="Invalid evaluation run_id.")
    return RUNS_ROOT / f"{run_id}.json"


def _atomic_write(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.stem}.{uuid.uuid4().hex}.tmp"
    encoded = json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8")
    try:
        temporary.write_bytes(encoded)
        for attempt in range(8):
            try:
                temporary.replace(path)
                break
            except PermissionError:
                if attempt == 7:
                    raise
                time.sleep(0.015 * (attempt + 1))
    finally:
        temporary.unlink(missing_ok=True)


def _validate_grader(grader: Any, case_id: str) -> Dict[str, Any]:
    if not isinstance(grader, Mapping) or grader.get("type") not in ALLOWED_GRADERS:
        raise HTTPException(status_code=500, detail=f"Suite {case_id} has an invalid grader.")
    result = dict(grader)
    weight = result.get("weight", 1)
    if not isinstance(weight, (int, float)) or isinstance(weight, bool) or weight <= 0:
        raise HTTPException(status_code=500, detail=f"Suite {case_id} has an invalid weight.")
    result["weight"] = min(float(weight), 100.0)
    grader_type = result["type"]
    if grader_type in {"contains", "not_contains", "starts_with", "regex", "tool_called"}:
        value = result.get("value")
        if not isinstance(value, str) or not value or len(value) > 500:
            raise HTTPException(status_code=500, detail=f"Suite {case_id} has an invalid grader value.")
    if grader_type == "artifact_count":
        minimum = result.get("min", 1)
        if not isinstance(minimum, int) or isinstance(minimum, bool) or minimum < 0:
            raise HTTPException(status_code=500, detail=f"Suite {case_id} has an invalid artifact minimum.")
    if grader_type == "latency_ms":
        maximum = result.get("max")
        if not isinstance(maximum, (int, float)) or maximum <= 0:
            raise HTTPException(status_code=500, detail=f"Suite {case_id} has an invalid latency maximum.")
    return result


def _validate_suite(value: Any, expected_id: Optional[str] = None) -> Dict[str, Any]:
    if not isinstance(value, Mapping):
        raise HTTPException(status_code=500, detail="Evaluation suite must be an object.")
    suite_id = value.get("id")
    if not isinstance(suite_id, str) or not SUITE_ID_RE.fullmatch(suite_id):
        raise HTTPException(status_code=500, detail="Evaluation suite has an invalid id.")
    if expected_id is not None and suite_id != expected_id:
        raise HTTPException(status_code=500, detail="Evaluation suite filename and id differ.")
    mode = value.get("mode", "agent")
    if mode not in ALLOWED_MODES:
        raise HTTPException(status_code=500, detail=f"Suite {suite_id} has an invalid mode.")
    raw_cases = value.get("cases")
    if not isinstance(raw_cases, list) or not raw_cases or len(raw_cases) > MAX_CASES:
        raise HTTPException(status_code=500, detail=f"Suite {suite_id} has an invalid case count.")
    cases: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for raw_case in raw_cases:
        if not isinstance(raw_case, Mapping):
            raise HTTPException(status_code=500, detail=f"Suite {suite_id} contains an invalid case.")
        case_id = raw_case.get("id")
        if not isinstance(case_id, str) or not SUITE_ID_RE.fullmatch(case_id) or case_id in seen:
            raise HTTPException(status_code=500, detail=f"Suite {suite_id} has an invalid case id.")
        seen.add(case_id)
        if mode == "agent" and not isinstance(raw_case.get("prompt"), str):
            raise HTTPException(status_code=500, detail=f"Case {case_id} has no prompt.")
        if mode == "fixture" and not isinstance(raw_case.get("fixture_output", ""), str):
            raise HTTPException(status_code=500, detail=f"Case {case_id} has invalid fixture output.")
        graders = raw_case.get("graders")
        if not isinstance(graders, list) or not graders:
            raise HTTPException(status_code=500, detail=f"Case {case_id} has no graders.")
        case = dict(raw_case)
        case["graders"] = [_validate_grader(grader, case_id) for grader in graders]
        threshold = case.get("pass_threshold", 0.8)
        if not isinstance(threshold, (int, float)) or not 0 <= threshold <= 1:
            raise HTTPException(status_code=500, detail=f"Case {case_id} has an invalid pass threshold.")
        case["pass_threshold"] = float(threshold)
        cases.append(case)
    return {
        "schema_version": 1,
        "id": suite_id,
        "name": str(value.get("name") or suite_id),
        "description": str(value.get("description") or ""),
        "mode": mode,
        "timeout_seconds": max(10, min(int(value.get("timeout_seconds", 90)), 300)),
        "cases": cases,
    }


def load_eval_suite(suite_id: str) -> Dict[str, Any]:
    path = _suite_path(suite_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Evaluation suite not found.")
    try:
        return _validate_suite(json.loads(path.read_text(encoding="utf-8")), suite_id)
    except HTTPException:
        raise
    except (OSError, ValueError, TypeError) as exc:
        raise HTTPException(status_code=500, detail="Evaluation suite is unreadable.") from exc


def list_eval_suites() -> Dict[str, Any]:
    suites: List[Dict[str, Any]] = []
    if SUITES_ROOT.is_dir():
        for path in sorted(SUITES_ROOT.glob("*.json")):
            if not SUITE_ID_RE.fullmatch(path.stem):
                continue
            suite = load_eval_suite(path.stem)
            suites.append(
                {
                    "id": suite["id"],
                    "name": suite["name"],
                    "description": suite["description"],
                    "mode": suite["mode"],
                    "case_count": len(suite["cases"]),
                    "timeout_seconds": suite["timeout_seconds"],
                }
            )
    return {"schema_version": 1, "suites": suites}


def eval_suite_info(suite_id: str) -> Dict[str, Any]:
    suite = load_eval_suite(suite_id)
    return {
        **{key: suite[key] for key in ("schema_version", "id", "name", "description", "mode", "timeout_seconds")},
        "case_count": len(suite["cases"]),
        "cases": [
            {
                "id": case["id"],
                "name": str(case.get("name") or case["id"]),
                "tags": list(case.get("tags") or []),
                "grader_count": len(case["graders"]),
            }
            for case in suite["cases"]
        ],
    }


def _activity_actions(sample: Mapping[str, Any]) -> set[str]:
    return {
        str(item.get("action"))
        for item in sample.get("activities", [])
        if isinstance(item, Mapping) and item.get("state") == "completed"
    }


def _grade_one(grader: Mapping[str, Any], sample: Mapping[str, Any]) -> Dict[str, Any]:
    grader_type = str(grader["type"])
    output = str(sample.get("output") or "")
    error = sample.get("error")
    passed = False
    observed: Any = None
    expected: Any = None
    if grader_type == "no_error":
        observed, expected, passed = error, None, not error
    elif grader_type == "output_nonempty":
        observed, expected, passed = len(output.strip()), "> 0", bool(output.strip())
    elif grader_type in {"contains", "not_contains", "starts_with"}:
        expected = str(grader["value"])
        case_sensitive = bool(grader.get("case_sensitive", False))
        haystack = output if case_sensitive else output.casefold()
        needle = expected if case_sensitive else expected.casefold()
        if grader_type == "contains":
            passed = needle in haystack
        elif grader_type == "not_contains":
            passed = needle not in haystack
        else:
            passed = haystack.lstrip().startswith(needle)
        observed = passed
    elif grader_type == "regex":
        expected = str(grader["value"])
        try:
            passed = re.search(expected, output, re.IGNORECASE if not grader.get("case_sensitive") else 0) is not None
        except re.error:
            passed = False
        observed = passed
    elif grader_type == "tool_called":
        expected = str(grader["value"])
        actions = sorted(_activity_actions(sample))
        observed, passed = actions, expected in actions
    elif grader_type == "artifact_count":
        expected = int(grader.get("min", 1))
        observed = len(sample.get("artifacts") or [])
        passed = observed >= expected
    elif grader_type == "latency_ms":
        expected = float(grader["max"])
        observed = float(sample.get("latency_ms") or 0)
        passed = observed <= expected and observed > 0
    return {
        "type": grader_type,
        "name": str(grader.get("name") or grader_type),
        "weight": grader["weight"],
        "passed": passed,
        "score": 1.0 if passed else 0.0,
        "observed": observed,
        "expected": expected,
    }


def grade_eval_sample(case: Mapping[str, Any], sample: Mapping[str, Any]) -> Dict[str, Any]:
    grades = [_grade_one(grader, sample) for grader in case["graders"]]
    total_weight = sum(float(grade["weight"]) for grade in grades)
    score = sum(float(grade["weight"]) * float(grade["score"]) for grade in grades) / total_weight
    threshold = float(case.get("pass_threshold", 0.8))
    return {"grades": grades, "score": round(score, 4), "passed": score >= threshold}


def _fixture_sample(case: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "output": str(case.get("fixture_output") or ""),
        "error": case.get("fixture_error"),
        "activities": list(case.get("fixture_activities") or []),
        "artifacts": list(case.get("fixture_artifacts") or []),
        "latency_ms": float(case.get("fixture_latency_ms", 1)),
    }


async def _consume_agent(payload: Dict[str, Any], config: AgentConfig) -> Dict[str, Any]:
    response = await run_agent(payload, config)
    if response.status_code >= 400:
        body = getattr(response, "body", b"")
        return {
            "output": "",
            "error": body.decode("utf-8", errors="replace")[:2000],
            "activities": [],
            "artifacts": [],
        }
    output_parts: List[str] = []
    activities: Dict[str, Dict[str, Any]] = {}
    artifacts: List[Dict[str, Any]] = []
    error: Optional[str] = None
    buffer = ""
    iterator = getattr(response, "body_iterator", None)
    if iterator is None:
        body = getattr(response, "body", b"")
        return {"output": body.decode("utf-8", errors="replace"), "error": None, "activities": [], "artifacts": []}
    async for chunk in iterator:
        if isinstance(chunk, bytes):
            buffer += chunk.decode("utf-8", errors="replace")
        else:
            buffer += str(chunk)
        while "\n\n" in buffer:
            event, buffer = buffer.split("\n\n", 1)
            data_lines = [line[5:].strip() for line in event.splitlines() if line.startswith("data:")]
            if not data_lines:
                continue
            data = "\n".join(data_lines)
            if not data or data == "[DONE]":
                continue
            try:
                packet = json.loads(data)
            except json.JSONDecodeError:
                continue
            if packet.get("error"):
                item = packet["error"]
                error = str(item.get("message") if isinstance(item, Mapping) else item)[:2000]
            activity = packet.get("agent_activity")
            if isinstance(activity, Mapping) and activity.get("id"):
                activities[str(activity["id"])] = dict(activity)
            artifact = packet.get("agent_artifact")
            if isinstance(artifact, Mapping):
                artifacts.append(dict(artifact))
            delta = (packet.get("choices") or [{}])[0].get("delta") or {}
            if delta.get("content"):
                output_parts.append(str(delta["content"]))
    return {
        "output": "".join(output_parts),
        "error": error,
        "activities": list(activities.values())[-MAX_STORED_ACTIVITIES:],
        "artifacts": artifacts[:20],
    }


def _default_reasoning_control(config: AgentConfig, model_id: str) -> Dict[str, Any]:
    selected = resolve_model(config, model_id)
    policy = REASONING_POLICIES.get(selected.profile.family)
    if not policy:
        return {}
    control: Dict[str, Any] = {"enabled": bool(policy["default_enabled"])}
    if control["enabled"] and policy["efforts"]:
        control["effort"] = policy["default_effort"]
    return control


async def _agent_sample(
    case: Mapping[str, Any], model_id: str, config: AgentConfig, timeout_seconds: int
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "messages": [{"role": "user", "content": str(case["prompt"])}],
        "stream": True,
        "temperature": float(case.get("temperature", 0)),
        "max_tokens": max(32, min(int(case.get("max_tokens", 600)), 4000)),
        "model_id": model_id,
        "reasoning_control": case.get("reasoning_control")
        or _default_reasoning_control(config, model_id),
    }
    for key in ("skill", "skill_mode", "paper_id"):
        if case.get(key) is not None:
            payload[key] = case[key]
    return await asyncio.wait_for(_consume_agent(payload, config), timeout=timeout_seconds)


async def _agent_sample_with_cancel(
    case: Mapping[str, Any],
    model_id: str,
    config: AgentConfig,
    timeout_seconds: int,
    cancel_event: threading.Event,
) -> Dict[str, Any]:
    task = asyncio.ensure_future(_agent_sample(case, model_id, config, timeout_seconds))
    try:
        while True:
            done, _ = await asyncio.wait({task}, timeout=0.25)
            if done:
                return task.result()
            if cancel_event.is_set():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                raise _EvalCancelled()
    finally:
        if not task.done():
            task.cancel()


def _sample_summary(
    run_id: str,
    case: Mapping[str, Any],
    model_id: str,
    repeat_index: int,
    sample: Dict[str, Any],
) -> Dict[str, Any]:
    output = str(sample.get("output") or "")
    sample["output"] = output[:MAX_STORED_OUTPUT_CHARS]
    grading = grade_eval_sample(case, sample)
    return {
        "id": f"sample_{uuid.uuid4().hex}",
        "run_id": run_id,
        "case_id": case["id"],
        "case_name": str(case.get("name") or case["id"]),
        "model_id": model_id,
        "repeat": repeat_index,
        **sample,
        **grading,
    }


def _aggregate(samples: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    if not samples:
        return {"sample_count": 0, "passed": 0, "pass_rate": 0.0, "average_score": 0.0, "average_latency_ms": 0}
    latencies = [float(sample.get("latency_ms") or 0) for sample in samples]
    return {
        "sample_count": len(samples),
        "passed": sum(bool(sample.get("passed")) for sample in samples),
        "pass_rate": round(sum(bool(sample.get("passed")) for sample in samples) / len(samples), 4),
        "average_score": round(statistics.fmean(float(sample.get("score") or 0) for sample in samples), 4),
        "average_latency_ms": round(statistics.fmean(latencies)),
    }


def _summarize(samples: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    model_ids = sorted({str(sample.get("model_id")) for sample in samples})
    return {
        "overall": _aggregate(samples),
        "models": {
            model_id: _aggregate([sample for sample in samples if sample.get("model_id") == model_id])
            for model_id in model_ids
        },
    }


async def _execute_run(
    run: Dict[str, Any], suite: Dict[str, Any], config: AgentConfig, cancel_event: threading.Event
) -> None:
    run["status"] = "running"
    run["started_at"] = _now()
    _atomic_write(_run_path(run["id"]), run)
    planned: List[Tuple[Mapping[str, Any], str, int]] = []
    models = run["model_ids"] if suite["mode"] == "agent" else ["fixture"]
    for model_id in models:
        for case in suite["cases"]:
            for repeat_index in range(1, run["repeat"] + 1):
                planned.append((case, model_id, repeat_index))
    try:
        for case, model_id, repeat_index in planned:
            if cancel_event.is_set():
                run["status"] = "cancelled"
                break
            started = time.perf_counter()
            try:
                if suite["mode"] == "fixture":
                    sample = _fixture_sample(case)
                else:
                    sample = await _agent_sample_with_cancel(
                        case, model_id, config, suite["timeout_seconds"], cancel_event
                    )
            except _EvalCancelled:
                run["status"] = "cancelled"
                break
            except asyncio.TimeoutError:
                sample = {"output": "", "error": "Evaluation sample timed out.", "activities": [], "artifacts": []}
            except Exception as exc:
                sample = {"output": "", "error": f"Evaluation sample failed: {exc}", "activities": [], "artifacts": []}
            measured_latency = max(1, round((time.perf_counter() - started) * 1000))
            if suite["mode"] == "agent" or "latency_ms" not in sample:
                sample["latency_ms"] = measured_latency
            run["samples"].append(
                _sample_summary(run["id"], case, model_id, repeat_index, sample)
            )
            run["completed_samples"] = len(run["samples"])
            run["summary"] = _summarize(run["samples"])
            run["updated_at"] = _now()
            _atomic_write(_run_path(run["id"]), run)
        if run["status"] == "running":
            run["status"] = "completed"
    except Exception as exc:
        run["status"] = "failed"
        run["error"] = str(exc)[:2000]
    finally:
        run["completed_at"] = _now()
        run["updated_at"] = run["completed_at"]
        run["summary"] = _summarize(run["samples"])
        _atomic_write(_run_path(run["id"]), run)


def _worker(
    run: Dict[str, Any], suite: Dict[str, Any], config: AgentConfig, cancel_event: threading.Event
) -> None:
    try:
        asyncio.run(_execute_run(run, suite, config, cancel_event))
    finally:
        with _LOCK:
            _ACTIVE.pop(run["id"], None)


def start_eval_run(payload: Any, config: AgentConfig) -> Dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise HTTPException(status_code=422, detail="Evaluation run payload must be an object.")
    suite = load_eval_suite(str(payload.get("suite_id") or ""))
    repeat = payload.get("repeat", 1)
    if not isinstance(repeat, int) or isinstance(repeat, bool) or not 1 <= repeat <= MAX_REPEATS:
        raise HTTPException(status_code=422, detail="repeat must be between 1 and 3.")
    if suite["mode"] == "fixture":
        model_ids: List[str] = []
    else:
        requested = payload.get("model_ids") or [config.default_model_id]
        if not isinstance(requested, list) or not requested or len(requested) > 12:
            raise HTTPException(status_code=422, detail="model_ids must be a non-empty array.")
        model_ids = []
        for value in requested:
            model_id = str(value)
            try:
                resolve_model(config, model_id)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            if model_id not in model_ids:
                model_ids.append(model_id)
    planned_samples = len(suite["cases"]) * repeat * (len(model_ids) if model_ids else 1)
    if planned_samples > MAX_SAMPLES:
        raise HTTPException(status_code=422, detail=f"Evaluation run exceeds {MAX_SAMPLES} samples.")
    with _LOCK:
        if any(thread.is_alive() for thread, _ in _ACTIVE.values()):
            raise HTTPException(status_code=409, detail="Another evaluation run is already active.")
        run_id = f"eval_{uuid.uuid4().hex}"
        run = {
            "schema_version": 1,
            "id": run_id,
            "suite_id": suite["id"],
            "suite_name": suite["name"],
            "mode": suite["mode"],
            "status": "queued",
            "model_ids": model_ids,
            "repeat": repeat,
            "planned_samples": planned_samples,
            "completed_samples": 0,
            "created_at": _now(),
            "updated_at": _now(),
            "samples": [],
            "summary": _summarize([]),
        }
        _atomic_write(_run_path(run_id), run)
        cancel_event = threading.Event()
        thread = threading.Thread(
            target=_worker,
            args=(run, suite, config, cancel_event),
            name=f"InferTreeEval-{run_id[-8:]}",
            daemon=True,
        )
        _ACTIVE[run_id] = (thread, cancel_event)
        thread.start()
    return {key: run[key] for key in (
        "id", "suite_id", "suite_name", "mode", "status", "model_ids",
        "repeat", "planned_samples", "completed_samples", "created_at",
    )}


def get_eval_run(run_id: str) -> Dict[str, Any]:
    path = _run_path(run_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Evaluation run not found.")
    try:
        run = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise HTTPException(status_code=500, detail="Evaluation run is unreadable.") from exc
    if run.get("status") in {"queued", "running"}:
        with _LOCK:
            active = run_id in _ACTIVE and _ACTIVE[run_id][0].is_alive()
        if not active and time.time() - path.stat().st_mtime > 5:
            # The worker writes its terminal state before removing itself from
            # _ACTIVE. Re-read after observing removal so a stale pre-replace
            # snapshot cannot be mislabeled as interrupted.
            try:
                refreshed = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                refreshed = run
            if refreshed.get("status") not in {"queued", "running"}:
                run = refreshed
            else:
                run["status"] = "interrupted"
                run["updated_at"] = _now()
                _atomic_write(path, run)
    return run


def list_eval_runs(limit: int = 20) -> Dict[str, Any]:
    safe_limit = max(1, min(int(limit), 100))
    paths = sorted(
        RUNS_ROOT.glob("eval_*.json") if RUNS_ROOT.is_dir() else [],
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )[:safe_limit]
    runs: List[Dict[str, Any]] = []
    for path in paths:
        try:
            run = get_eval_run(path.stem)
        except HTTPException:
            continue
        runs.append(
            {
                key: run.get(key)
                for key in (
                    "id", "suite_id", "suite_name", "mode", "status", "model_ids",
                    "repeat", "planned_samples", "completed_samples", "created_at",
                    "updated_at", "summary",
                )
            }
        )
    return {"schema_version": 1, "runs": runs}


def cancel_eval_run(run_id: str) -> Dict[str, Any]:
    _run_path(run_id)
    with _LOCK:
        active = _ACTIVE.get(run_id)
        if active is None or not active[0].is_alive():
            run = get_eval_run(run_id)
            return {"id": run_id, "status": run["status"], "cancel_requested": False}
        active[1].set()
    return {"id": run_id, "status": "cancelling", "cancel_requested": True}
