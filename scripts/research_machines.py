#!/usr/bin/env python3
"""Research exactly one machine per worker using Gemini + Google Search grounding."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from google import genai
from google.genai import types
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]
BACKLOG_PATH = ROOT / "machines_backlog.json"
MACHINE_SCHEMA_PATH = ROOT / "machine.schema.json"
AGENT_PATH = ROOT / "AGENT_RESEARCHER.md"

DEFAULT_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash-lite")
DEFAULT_BATCH_SIZE = int(os.getenv("RESEARCH_BATCH_SIZE", "12"))
DEFAULT_WORKERS = int(os.getenv("RESEARCH_WORKERS", "6"))
MAX_RETRIES = 4


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    tmp.replace(path)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def select_research_machines(backlog: dict[str, Any], batch_size: int) -> list[dict[str, Any]]:
    # Recover jobs left in researching by an interrupted previous run.
    # Workflow concurrency prevents overlapping research runs.
    for machine in backlog["machines"]:
        if machine["status"] == "researching":
            machine["status"] = "open"

    jobs = [m for m in backlog["machines"] if m["status"] == "open"]
    return jobs[:batch_size]


def resolve_refs(schema: Any, root_schema: dict[str, Any]) -> Any:
    if isinstance(schema, list):
        return [resolve_refs(x, root_schema) for x in schema]
    if not isinstance(schema, dict):
        return schema
    if "$ref" in schema:
        if schema["$ref"] == "#/$defs/axis":
            schema = root_schema["$defs"]["axis"]
        else:
            raise ValueError(f"Unsupported schema ref: {schema['$ref']}")

    allowed = {"type", "properties", "required", "items", "description", "minLength", "minimum", "maximum"}
    result = {k: resolve_refs(v, root_schema) for k, v in schema.items() if k in allowed}
    return result


def generation_schema() -> dict[str, Any]:
    """Build a Gemini-compatible output schema.

    Gemini's response_schema API does not accept JSON Schema's required
    lists in this form. We enforce all required fields locally with the
    canonical machine.schema.json after generation instead.
    """
    original = load_json(MACHINE_SCHEMA_PATH)
    schema = resolve_refs(original, original)

    def strip_required(value: Any) -> Any:
        if isinstance(value, list):
            return [strip_required(item) for item in value]
        if not isinstance(value, dict):
            return value
        return {
            key: strip_required(item)
            for key, item in value.items()
            if key != "required"
        }

    return strip_required(schema)


def validate_machine(data: dict[str, Any], expected: dict[str, Any]) -> list[str]:
    errors = [
        f"{'.'.join(str(p) for p in e.path) or '<root>'}: {e.message}"
        for e in Draft202012Validator(load_json(MACHINE_SCHEMA_PATH)).iter_errors(data)
    ]

    if data.get("id") != expected["id"]:
        errors.append(f"id mismatch: expected {expected['id']!r}")

    for key in ("manufacturer", "series", "model"):
        actual = data.get(key, {})
        if not isinstance(actual, dict) or actual.get("name") != expected[key]:
            errors.append(f"{key}.name mismatch: expected {expected[key]!r}")

    if data.get("variant") != expected.get("variant"):
        errors.append(f"variant mismatch: expected {expected.get('variant')!r}")

    if not data.get("source", {}).get("urls"):
        errors.append("source.urls is empty")

    return errors


def grounding_urls(response: Any) -> list[str]:
    urls: list[str] = []
    try:
        metadata = getattr(response.candidates[0], "grounding_metadata", None)
        for chunk in getattr(metadata, "grounding_chunks", None) or []:
            web = getattr(chunk, "web", None)
            uri = getattr(web, "uri", None)
            if uri and uri not in urls:
                urls.append(uri)
    except Exception:
        pass
    return urls


def build_prompt(machine: dict[str, Any], existing: dict[str, Any] | None, instructions: str) -> str:
    existing_text = json.dumps(existing, ensure_ascii=False, indent=2) if existing else "Keine bestehende Datei."
    return f"""
{instructions}

Du bist jetzt ausschließlich für diese Maschine zuständig:

machine_id: {machine["id"]}
manufacturer: {machine["manufacturer"]}
series: {machine["series"]}
model: {machine["model"]}
variant: {machine["variant"]}
target_file: {machine["data_file"]}

Bestehende Zieldaten, falls vorhanden:
{existing_text}

Arbeitsauftrag:
- Recherchiere ausschließlich dieses exakte Modell und diese Variante.
- Nutze Google Search Grounding.
- Priorisiere offizielle Herstellerquellen.
- Verwende nur belastbar belegte Werte.
- Unbekannte Werte sind null.
- Bei nicht auflösbaren Widersprüchen: status = needs_review.
- Gib ausschließlich ein einzelnes JSON-Objekt nach dem Maschinen-Schema zurück.
- Keine Markdown-Codeblöcke und keine Erklärung außerhalb des JSON.
- Erfinde keine URLs.
"""


def research_one(machine: dict[str, Any], instructions: str, model: str) -> dict[str, Any]:
    client = genai.Client()
    target = ROOT / machine["data_file"]
    existing = load_json(target) if target.exists() else None

    config = types.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema=generation_schema(),
        temperature=0.1,
        max_output_tokens=8192,
        tools=[types.Tool(google_search=types.GoogleSearch())],
    )

    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = client.models.generate_content(
                model=model,
                contents=build_prompt(machine, existing, instructions),
                config=config,
            )
            if not response.text:
                raise RuntimeError("Gemini lieferte keinen Text.")

            data = json.loads(response.text)
            urls = grounding_urls(response)

            data.setdefault("source", {})
            if urls:
                data["source"]["urls"] = urls
                data["source"]["verified"] = True
                data["source"].setdefault("type", "google_search_grounded")
            else:
                data["source"]["verified"] = False

            errors = validate_machine(data, machine)
            if errors:
                raise ValueError(" | ".join(errors))

            return {"machine": machine, "data": data, "error": None}

        except Exception as exc:
            last_error = exc
            if attempt < MAX_RETRIES:
                delay = min(30, 2 ** attempt)
                print(f"[{machine['id']}] Versuch {attempt} fehlgeschlagen: {exc}; retry in {delay}s", flush=True)
                time.sleep(delay)

    return {"machine": machine, "data": None, "error": str(last_error)}


def validate_backlog_only() -> int:
    backlog = load_json(BACKLOG_PATH)
    errors = list(Draft202012Validator(load_json(ROOT / "backlog.schema.json")).iter_errors(backlog))
    if errors:
        for e in errors:
            print(f"BACKLOG ERROR {'.'.join(str(p) for p in e.path)}: {e.message}", file=sys.stderr)
        return 1
    print(f"Backlog OK: {len(backlog['machines'])} Maschinen.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    args = parser.parse_args()

    if args.validate_only:
        return validate_backlog_only()

    if not os.getenv("GEMINI_API_KEY"):
        print("GEMINI_API_KEY fehlt. Bitte als GitHub Actions Secret setzen.", file=sys.stderr)
        return 2

    if args.batch_size < 1 or args.workers < 1:
        print("batch-size und workers müssen >= 1 sein.", file=sys.stderr)
        return 2

    backlog = load_json(BACKLOG_PATH)
    jobs = select_research_machines(backlog, args.batch_size)

    if not jobs:
        print("Keine offenen Maschinen mehr.")
        return 0

    # Mark jobs as researching before the external calls start.
    for machine in backlog["machines"]:
        if machine["id"] in {j["id"] for j in jobs}:
            machine["status"] = "researching"
    backlog["last_updated"] = now_iso()
    save_json(BACKLOG_PATH, backlog)

    instructions = AGENT_PATH.read_text(encoding="utf-8")
    print(f"Starte {len(jobs)} Maschinen mit {min(args.workers, len(jobs))} parallelen Workern.", flush=True)

    results = []
    with ThreadPoolExecutor(max_workers=min(args.workers, len(jobs))) as executor:
        futures = {
            executor.submit(research_one, machine, instructions, args.model): machine
            for machine in jobs
        }
        for future in as_completed(futures):
            machine = futures[future]
            try:
                result = future.result()
            except Exception as exc:
                result = {"machine": machine, "data": None, "error": str(exc)}
            results.append(result)
            if result["data"] is None:
                print(f"[FAILED] {machine['id']}: {result['error']}", flush=True)
            else:
                print(f"[DONE] {machine['id']} -> {machine['data_file']}", flush=True)

    by_id = {r["machine"]["id"]: r for r in results}
    completed = failed = 0

    for machine in backlog["machines"]:
        result = by_id.get(machine["id"])
        if not result:
            continue
        if result["data"] is None:
            machine["status"] = "failed"
            failed += 1
            continue

        save_json(ROOT / machine["data_file"], result["data"])
        machine["status"] = "needs_review" if result["data"].get("status") == "needs_review" else "completed"
        completed += 1

    backlog["last_updated"] = now_iso()
    save_json(BACKLOG_PATH, backlog)

    open_count = sum(1 for m in backlog["machines"] if m["status"] == "open")
    print(f"Fertig: {completed}; fehlgeschlagen: {failed}; noch open: {open_count}", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
