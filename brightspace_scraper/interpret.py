"""Stage 2: AI interpretation — turn harvested items into reconciled deadlines.

The interpreter sees ALL of a course's items together (structured + unstructured) so it
can reconcile (an announcement may override a due date) and tell real deadlines from
calendar/lecture noise. The structured due date is passed as a TRUSTED field: the model
confirms it, or overrides it only with clear textual evidence — never invents one. Output
keeps both the original structured date and the model's final date, with confidence and a
source pointer, so any change is auditable.

Provider-agnostic: LocalInterpreter targets the OpenAI-compatible chat endpoint that
Ollama / LM Studio / llama.cpp / vLLM all expose, so the same code runs on any of them and
a cloud backend can be swapped in later behind the same interface.
"""

from __future__ import annotations

import json
import os
import re
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass

import httpx

from .ocr import ocr_available, ocr_file

# Item types that can carry a deadline worth surfacing.
_DEADLINE_TYPES = {"assignment", "quiz", "calendar_event", "announcement",
                   "content_page", "content_file"}
# Bundle ordering: assignments/quizzes/announcements first (always kept), then content by
# relevance (lab sheets/syllabi before lecture slides), calendar last.
_TIER = {"assignment": 0, "quiz": 1, "announcement": 2, "content_file": 3,
         "content_page": 3, "calendar_event": 5}
_PER_ITEM_TEXT_CHARS = 2200
# Syllabi / outlines / assignment & lab sheets are date-dense — give them much more room
# so the schedule (which usually sits well past 2k chars) actually reaches the model.
_HIGH_VALUE_RE = re.compile(r"outline|syllab|assignment|\blab\b|project|problem set|schedule",
                            re.I)
_HIGH_VALUE_TEXT_CHARS = 9000
_COURSE_CHAR_BUDGET = 24000

# Words that signal a document likely carries a deadline (boost into the budget) ...
_DEADLINE_HINTS = ("due", "deadline", "submit", "submission", "assignment", "lab",
                   "project", "report", "quiz", "exam", "midterm", "milestone",
                   "schedule", "outline", "syllabus", "problem set", "week")
# ... and ones that signal pure reference/lecture noise (push out of the budget first).
_NOISE_HINTS = ("datasheet", "lecture", "slides", "lesson", "notes", "solution",
                "answer", "tutorial")


def _content_relevance(item: dict) -> int:
    """Heuristic: higher = more likely to contain a deadline. Used only to order
    content within the budget; assignments/quizzes/announcements always rank above."""
    hay = ((item.get("title") or "") + " " + (item.get("extracted_text") or "")[:600]).lower()
    score = sum(2 for w in _DEADLINE_HINTS if w in hay)
    score -= sum(2 for w in _NOISE_HINTS if w in hay)
    return score

SYSTEM_PROMPT = """\
You are a teaching assistant extracting graded DEADLINES and ASSESSMENTS from one course's
materials (assignments, quizzes, announcements, and documents like the course
outline/syllabus and lab/assignment sheets). Each item has a type, a title, an optional
STRUCTURED_DUE_DATE (set by the system — authoritative if present), a source URL, and text.

CAPTURE every graded item that has a date, including:
- assignments, labs, projects, reports, problem sets that are submitted, AND
- quizzes, tests, term tests, midterms, and exams — these ARE deadlines. Capture them even
  when they appear inside a course-outline grading table or weekly schedule.

IMPORTANT details:
- A single line can list MULTIPLE dates. "Quizzes (3): June 5, June 19, July 17" -> output
  THREE deadlines (Quiz 1, Quiz 2, Quiz 3). "Term Tests (2): June 15; July 15" -> two.
- Dates often omit the year. Infer the year from other dated items in this course (e.g. a
  STRUCTURED_DUE_DATE) — these are current-term courses; do not output a past year.
- If STRUCTURED_DUE_DATE is set, use it unless the text clearly overrides it. Never invent
  a date with no textual basis.
- Resolve relative dates ("next Friday") only if an anchor date is present; otherwise set
  final_due_date to null and confidence "low".
- IGNORE non-graded lecture/topic rows, readings, tutorials, and office hours. But DO
  capture any graded quiz/test/midterm/exam/assignment even if it sits in a schedule.
- Use a clear title: "Quiz 2", "Midterm Exam", "Term Test 1", "Lab 2 Report".

Return ONLY JSON of the form:
{"deadlines": [
  {"item_id": "<source item id>", "title": "<short>",
   "type": "assignment|quiz|test|midterm|exam|lab|project|other",
   "final_due_date": "YYYY-MM-DDTHH:MM:SSZ or null", "confidence": "high|medium|low",
   "reasoning": "<one sentence: where the date came from / why>"}
]}
"""


@dataclass
class Deadline:
    org_unit_id: int
    item_id: str | None
    title: str
    type: str
    final_due_date: str | None
    structured_due_date: str | None
    confidence: str
    source_url: str | None
    reasoning: str


def _truncate(text: str | None, limit: int) -> str:
    if not text:
        return ""
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + " …[truncated]"


def ocr_pass(items: list[dict]) -> int:
    """Interpreter-side OCR: fill text for image/scanned items from their raw file.

    Mutates items in place — sets `extracted_text` and clears `needs_vision` when OCR
    yields text. Runs wherever Stage 2 runs (the GPU desktop, eventually). No-op if
    Tesseract isn't installed or the raw file isn't reachable on this machine. Returns
    the number of items OCR'd. (Runs each interpret; cache by file hash later for dedup.)
    """
    if not ocr_available():
        return 0
    from pathlib import Path

    done = 0
    for it in items:
        if it.get("body_text") or it.get("extracted_text") or not it.get("needs_vision"):
            continue
        ref = it.get("content_ref")
        if not ref or not Path(ref).exists():
            continue
        text = ocr_file(Path(ref))
        if text:
            it["extracted_text"] = text
            it["needs_vision"] = False
            done += 1
    return done


def render_bundle(course_name: str, items: list[dict]) -> str:
    """Compact, token-bounded rendering of a course's items for the model."""
    lines: list[str] = [f"COURSE: {course_name}", ""]
    budget = _COURSE_CHAR_BUDGET
    dropped = 0

    def sort_key(i: dict):
        t = _TIER.get(i.get("type"), 9)
        rel = -_content_relevance(i) if i.get("type") in ("content_file", "content_page") else 0
        return (t, rel)

    for it in sorted(items, key=sort_key):
        if it.get("type") not in _DEADLINE_TYPES:
            continue
        text = it.get("body_text") or it.get("extracted_text") or ""
        limit = (_HIGH_VALUE_TEXT_CHARS
                 if _HIGH_VALUE_RE.search(it.get("title") or "")
                 else _PER_ITEM_TEXT_CHARS)
        block = (
            f"- item_id: {it.get('id')}\n"
            f"  type: {it.get('type')}\n"
            f"  title: {it.get('title')}\n"
            f"  STRUCTURED_DUE_DATE: {it.get('structured_due_date') or 'none'}\n"
            f"  text: {_truncate(text, limit)}\n"
        )
        if len(block) > budget:
            dropped += 1
            continue
        lines.append(block)
        budget -= len(block)
    if dropped:
        lines.append(f"\n[note: {dropped} item(s) omitted to fit context budget]")
    return "\n".join(lines)


def _parse_model_json(raw: str) -> dict:
    """Parse the model's JSON, salvaging the outermost object if it's wrapped in prose.

    Never raises: a malformed/empty/non-JSON response (e.g. a model that ran out of
    context and emitted prose) degrades to no model deadlines, so the course still gets
    its deterministically-seeded structured dates instead of crashing the whole run.
    """
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        pass
    start, end = raw.find("{"), raw.rfind("}")
    if start >= 0 < end and start < end:
        try:
            return json.loads(raw[start:end + 1])
        except json.JSONDecodeError:
            pass
    return {"deadlines": []}


def _build_deadlines(
    org_unit_id: int, raw_deadlines: list[dict], items: list[dict]
) -> list[Deadline]:
    """Turn the model's raw deadline dicts into Deadlines — applying the structured-date
    safety net, deterministic seeding, and dedup. Shared by every backend."""
    by_id = {it.get("id"): it for it in items}
    results: list[Deadline] = []
    for d in raw_deadlines:
        src = by_id.get(d.get("item_id"), {})
        structured = src.get("structured_due_date")
        final = d.get("final_due_date")
        confidence = d.get("confidence") or "low"
        reasoning = d.get("reasoning") or ""

        # Safety net: never lose a known date. If the model dropped the final date but the
        # system had a structured one, the structured date wins (it's authoritative unless
        # text clearly overrides it).
        if not final and structured:
            final = structured
            confidence = "high"
            reasoning = f"[fallback to structured date] {reasoning}".strip()

        results.append(Deadline(
            org_unit_id=org_unit_id,
            item_id=d.get("item_id"),
            title=d.get("title") or src.get("title") or "(untitled)",
            type=d.get("type") or "other",
            final_due_date=final,
            structured_due_date=structured,
            confidence=confidence,
            source_url=src.get("source_url"),
            reasoning=reasoning,
        ))

    # Deterministic seed: every assignment/quiz with a structured due date must appear,
    # even if the model omitted it (small models drop items in noisy context). Ground-truth
    # dates never depend on the model; the model only adds/reconciles.
    present = {r.item_id for r in results}
    for it in items:
        if (it.get("type") in ("assignment", "quiz")
                and it.get("structured_due_date")
                and it.get("id") not in present):
            results.append(Deadline(
                org_unit_id=org_unit_id,
                item_id=it.get("id"),
                title=it.get("title") or "(untitled)",
                type=it.get("type"),
                final_due_date=it.get("structured_due_date"),
                structured_due_date=it.get("structured_due_date"),
                confidence="high",
                source_url=it.get("source_url"),
                reasoning="[seeded] structured due date from Brightspace",
            ))
    return _dedupe(results)


class Interpreter(ABC):
    """Base class. Subclasses implement only `_complete()`; bundling, JSON parsing,
    the structured-date safety net, seeding, and dedup are shared here so a new backend
    (e.g. a cloud model) just makes the model call."""

    @abstractmethod
    def _complete(self, system: str, user: str) -> str:
        """Send (system, user) to the model and return its raw text response."""

    def interpret_course(
        self, org_unit_id: int, course_name: str, items: list[dict]
    ) -> list[Deadline]:
        ocr_pass(items)  # interpreter-side OCR of any scanned/image items
        bundle = render_bundle(course_name, items)
        raw = self._complete(SYSTEM_PROMPT, bundle)
        parsed = _parse_model_json(raw)
        return _build_deadlines(org_unit_id, parsed.get("deadlines", []), items)


class LocalInterpreter(Interpreter):
    """Talks to an OpenAI-compatible local server (Ollama/LM Studio/llama.cpp/vLLM)."""

    def __init__(self, base_url: str | None = None, model: str | None = None,
                 timeout: float = 600.0, stream: bool = False):
        self.base_url = (base_url or os.environ.get(
            "LOCAL_LLM_URL", "http://localhost:11434/v1")).rstrip("/")
        self.model = model or os.environ.get("LOCAL_LLM_MODEL", "llama3.1")
        self.timeout = timeout
        self.stream = stream

    def _complete(self, system: str, user: str) -> str:
        return self._chat(system, user)

    def _payload(self, system: str, user: str, stream: bool) -> dict:
        return {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "stream": stream,
        }

    def _chat(self, system: str, user: str) -> str:
        if self.stream:
            return self._chat_streaming(system, user)
        resp = httpx.post(
            f"{self.base_url}/chat/completions",
            json=self._payload(system, user, False),
            timeout=self.timeout,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]

    def _chat_streaming(self, system: str, user: str) -> str:
        """Stream tokens to stdout live (so you can watch the model generate)."""
        import sys

        parts: list[str] = []
        with httpx.stream(
            "POST",
            f"{self.base_url}/chat/completions",
            json=self._payload(system, user, True),
            timeout=self.timeout,
        ) as resp:
            resp.raise_for_status()
            for line in resp.iter_lines():
                if not line or not line.startswith("data:"):
                    continue
                data = line[len("data:"):].strip()
                if data == "[DONE]":
                    break
                try:
                    obj = json.loads(data)
                except json.JSONDecodeError:
                    continue
                delta = (obj.get("choices") or [{}])[0].get("delta", {}).get("content")
                if delta:
                    parts.append(delta)
                    sys.stdout.write(delta)
                    sys.stdout.flush()
        sys.stdout.write("\n")
        sys.stdout.flush()
        return "".join(parts)


class OllamaInterpreter(Interpreter):
    """Ollama's NATIVE /api/chat backend.

    Reasoning models (Qwen3.x) default to a 'thinking' mode that can consume the entire
    response as hidden reasoning and return EMPTY content. The OpenAI-compatible /v1
    endpoint gives no way to turn thinking off, but Ollama's native /api/chat accepts
    `think: false`. Everything else (bundling, parsing, seeding) is inherited; only the
    model call differs — so this slots in beside LocalInterpreter per the backend rule.
    """

    def __init__(self, base_url: str | None = None, model: str | None = None,
                 timeout: float = 600.0, think: bool = False):
        url = (base_url or os.environ.get(
            "LOCAL_LLM_URL", "http://localhost:11434/v1")).rstrip("/")
        # native API lives at the server root, not under /v1
        self.root = url[:-3].rstrip("/") if url.endswith("/v1") else url
        self.model = model or os.environ.get("LOCAL_LLM_MODEL", "qwen3.5:4b-q4_K_M")
        self.timeout = timeout
        self.think = think

    def _complete(self, system: str, user: str) -> str:
        resp = httpx.post(
            f"{self.root}/api/chat",
            json={
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "think": self.think,        # the switch /v1 can't reach
                "format": "json",           # constrain output to valid JSON
                "stream": False,
                "options": {"temperature": 0},
            },
            timeout=self.timeout,
        )
        resp.raise_for_status()
        return resp.json().get("message", {}).get("content", "")


def _dedupe(deadlines: list[Deadline]) -> list[Deadline]:
    """Collapse duplicates — e.g. a calendar event mirroring an assignment.

    Keyed by (normalized title, due-date day). Prefer the assignment/quiz source over a
    calendar_event, and higher confidence, when merging.
    """
    rank = {"assignment": 0, "quiz": 1, "exam": 1, "lab": 1, "project": 1,
            "calendar_event": 5, "other": 6}
    conf_rank = {"high": 0, "medium": 1, "low": 2}
    best: dict[tuple, Deadline] = {}
    for d in deadlines:
        title_key = "".join(ch for ch in (d.title or "").lower() if ch.isalnum())
        day = (d.final_due_date or "")[:10]
        key = (title_key, day)
        cur = best.get(key)
        if cur is None:
            best[key] = d
            continue
        # keep the better-typed / more-confident one
        if (rank.get(d.type, 6), conf_rank.get(d.confidence, 3)) < \
           (rank.get(cur.type, 6), conf_rank.get(cur.confidence, 3)):
            best[key] = d
    return list(best.values())


def deadlines_to_dicts(deadlines: list[Deadline]) -> list[dict]:
    return [asdict(d) for d in deadlines]


# --------------------------------------------------------------------------- CLI
def _target_courses(store, args) -> list[int]:
    if args.courses:
        return [int(x) for x in args.courses.split(",") if x.strip()]
    if args.all:
        return store.all_course_ids()
    # default: courses touched by the latest changeset
    from .config import load_config
    cfg = load_config()
    cs_path = cfg.data_dir / "changeset.json"
    if not cs_path.exists():
        return store.all_course_ids()
    cs = json.loads(cs_path.read_text())
    ous = {i["org_unit_id"] for i in cs.get("new", []) + cs.get("changed", [])}
    return sorted(ous)


def main(argv: list[str] | None = None) -> int:
    import argparse
    import sys

    from .config import load_config
    from .store import Store

    p = argparse.ArgumentParser(prog="brightspace_scraper.interpret")
    p.add_argument("--all", action="store_true", help="interpret all courses in the store")
    p.add_argument("--courses", help="comma-separated org_unit_ids")
    p.add_argument("--dry-run", action="store_true", help="build bundles, no model call")
    p.add_argument("--model", help="local model name (default $LOCAL_LLM_MODEL or llama3.1)")
    p.add_argument("--url", help="OpenAI-compatible base URL (default Ollama)")
    p.add_argument("--stream", action="store_true",
                   help="stream the model's output live to the terminal")
    p.add_argument("--backend", choices=["openai", "ollama"],
                   help="LLM backend (default $LLM_BACKEND or 'openai'). 'ollama' uses "
                        "the native /api/chat so thinking can be disabled on Qwen3.x")
    args = p.parse_args(argv if argv is not None else sys.argv[1:])

    cfg = load_config()
    store = Store(cfg.db_path)
    ids = _target_courses(store, args)
    items_by_course = store.get_items_for_courses(ids)
    print(f"Interpreting {len(ids)} course(s)...")

    if args.dry_run:
        total = 0
        for oid in ids:
            items = items_by_course.get(oid, [])
            ocr_pass(items)  # so token sizing reflects OCR'd text too
            bundle = render_bundle(store.course_name(oid) or str(oid), items)
            total += len(bundle)
            print(f"  [{oid}] {store.course_name(oid) or '':40.40} "
                  f"items={len(items):3} bundle={len(bundle):6} chars (~{len(bundle)//4} tok)")
        print(f"\nDRY RUN — no model called. Total ~{total//4} tokens across courses.")
        store.close()
        return 0

    backend = (args.backend or os.environ.get("LLM_BACKEND", "openai")).lower()
    if backend == "ollama":
        interp = OllamaInterpreter(base_url=args.url, model=args.model)
        print(f"Using Ollama model '{interp.model}' at {interp.root}/api/chat "
              f"(think=off)")
    else:
        interp = LocalInterpreter(base_url=args.url, model=args.model, stream=args.stream)
        print(f"Using local model '{interp.model}' at {interp.base_url}"
              + (" [streaming]" if args.stream else ""))
    all_dl: list[Deadline] = []
    for oid in ids:
        items = items_by_course.get(oid, [])
        if not items:
            continue
        if args.stream:
            print(f"\n===== {store.course_name(oid) or oid} — model output =====")
        try:
            dls = interp.interpret_course(oid, store.course_name(oid) or str(oid), items)
        except Exception as exc:
            print(f"  ! interpret failed for {oid}: {type(exc).__name__}: {exc}",
                  file=sys.stderr)
            continue
        all_dl.extend(dls)
        print(f"  [{oid}] {store.course_name(oid) or '':36.36} -> {len(dls)} deadline(s)")

    run_id = store.start_run()
    store.save_deadlines(deadlines_to_dicts(all_dl), run_id)
    store.finish_run(run_id)
    store.close()

    out = cfg.data_dir / "deadlines.json"
    out.write_text(json.dumps(deadlines_to_dicts(all_dl), indent=2), encoding="utf-8")
    by_conf: dict[str, int] = {}
    for d in all_dl:
        by_conf[d.confidence] = by_conf.get(d.confidence, 0) + 1
    print(f"\n{len(all_dl)} deadline(s) by confidence: {by_conf}")
    print(f"deadlines -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
