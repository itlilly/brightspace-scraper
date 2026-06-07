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
# Per-CHUNK budget. A course's items are split into bundles of ~this size and interpreted
# one chunk at a time, then merged — instead of one big truncated bundle. Small models lose
# recall when a single dated announcement is buried in a 16k-char firehose of 273 items
# (proven: the same 4B model returned {"deadlines": []} for a whole course at 16k, but
# nailed the quiz when handed just the 2k-char announcements). Smaller chunks = better
# attention AND no truncation/data-loss (every item is seen across some chunk). ~6k chars
# (~2k tokens) leaves ample room for the system prompt + JSON output. A single high-value
# item (syllabus, up to _HIGH_VALUE_TEXT_CHARS) may exceed this and gets its own chunk.
_CHUNK_CHAR_BUDGET = 3000

# Words that signal a document likely carries a deadline (boost into the budget) ...
_DEADLINE_HINTS = ("due", "deadline", "submit", "submission", "assignment", "lab",
                   "project", "report", "quiz", "exam", "midterm", "milestone",
                   "schedule", "outline", "syllabus", "problem set", "week")
# ... and ones that signal pure reference/lecture noise (push out of the budget first).
_NOISE_HINTS = ("datasheet", "lecture", "slides", "lesson", "notes", "solution",
                "answer", "tutorial")

# Deterministic backstop for non-deadlines the model sometimes captures anyway: solution/
# answer postings, lecture recordings, class sessions. Conservative on purpose — we match
# clearly-material words (not bare "class"/"test"/"quiz") so we never drop a real assessment.
# Under-filtering beats over-filtering: a stray entry is annoying; deleting a real exam is bad.
_NOISE_TITLE_RE = re.compile(
    r"\b(solutions?|soln|answers?|lecture|lesson|recording)\b|_ans[_\d]", re.I)


def _is_noise_deadline(title: str | None, dtype: str | None) -> bool:
    """True if this looks like posted material, not a graded deadline."""
    if (dtype or "").lower() == "class":   # a type the model invents for lecture sessions
        return True
    return bool(_NOISE_TITLE_RE.search(title or ""))


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
- IGNORE non-graded lecture/topic rows, readings, tutorials, and office hours. Also IGNORE
  solution/answer postings, lecture recordings, and class sessions — a "Quiz 1 Solution",
  a posted answer key, or a lecture is NOT a deadline. But DO capture any graded
  quiz/test/midterm/exam/assignment even if it sits in a schedule.
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


def _item_blocks(items: list[dict]) -> list[str]:
    """Render a course's deadline-bearing items to ordered, atomic text blocks.

    High-value / most-relevant items first (so the most useful content leads each chunk).
    No budget cap here — packing into chunks happens in `render_chunks`; nothing is dropped.
    """
    def sort_key(i: dict):
        t = _TIER.get(i.get("type"), 9)
        rel = -_content_relevance(i) if i.get("type") in ("content_file", "content_page") else 0
        return (t, rel)

    blocks: list[str] = []
    for it in sorted(items, key=sort_key):
        if it.get("type") not in _DEADLINE_TYPES:
            continue
        text = it.get("body_text") or it.get("extracted_text") or ""
        limit = (_HIGH_VALUE_TEXT_CHARS
                 if _HIGH_VALUE_RE.search(it.get("title") or "")
                 else _PER_ITEM_TEXT_CHARS)
        blocks.append(
            f"- item_id: {it.get('id')}\n"
            f"  type: {it.get('type')}\n"
            f"  title: {it.get('title')}\n"
            f"  STRUCTURED_DUE_DATE: {it.get('structured_due_date') or 'none'}\n"
            f"  text: {_truncate(text, limit)}\n"
        )
    return blocks


def render_chunks(course_name: str, items: list[dict], *,
                  budget: int = _CHUNK_CHAR_BUDGET) -> list[str]:
    """Split a course's items into model-sized bundles ("chunks").

    Small models lose recall when one dated item is buried in a huge bundle, so instead of
    one big (truncated) bundle we pack the ordered item blocks into ~`budget`-char chunks and
    interpret each. Two guarantees: every item is seen across some chunk (no truncation/data
    loss), and **a single item is never split** — a block bigger than `budget` simply gets its
    own chunk. The caller merges + dedups results across chunks.
    """
    header = f"COURSE: {course_name}"
    chunks: list[str] = []
    cur: list[str] = []
    size = 0
    for block in _item_blocks(items):
        if cur and size + len(block) > budget:
            chunks.append(header + "\n\n" + "\n".join(cur))
            cur, size = [], 0
        cur.append(block)
        size += len(block)
    if cur:
        chunks.append(header + "\n\n" + "\n".join(cur))
    return chunks or [header + "\n\n(no deadline-bearing items)"]


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
    dropped: list[str] = []
    for d in raw_deadlines:
        src = by_id.get(d.get("item_id"), {})
        title = d.get("title") or src.get("title") or "(untitled)"
        if _is_noise_deadline(title, d.get("type")):
            dropped.append(title)
            continue
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
            title=title,
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
                and it.get("id") not in present
                and not _is_noise_deadline(it.get("title"), it.get("type"))):
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
    if dropped:
        print(f"  [filtered {len(dropped)} non-deadline(s): "
              f"{', '.join(dropped[:5])}{' …' if len(dropped) > 5 else ''}]")
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
        # Interpret each chunk separately so no item is buried in a giant bundle, then merge.
        raw_deadlines: list[dict] = []
        for chunk in render_chunks(course_name, items):
            parsed = _parse_model_json(self._complete(SYSTEM_PROMPT, chunk))
            raw_deadlines.extend(parsed.get("deadlines", []))
        return _build_deadlines(org_unit_id, raw_deadlines, items)


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


def _norm_title(title: str | None) -> str:
    """Lowercase, alphanumerics only — the comparison key for titles."""
    return "".join(ch for ch in (title or "").lower() if ch.isalnum())


def _dedupe(deadlines: list[Deadline]) -> list[Deadline]:
    """Collapse duplicates of the same real deadline.

    Two deadlines on the **same due-day** are merged when one normalized title *contains*
    the other — so a calendar event mirroring an assignment, or the same quiz described two
    ways across chunks ("Quiz 2" from the syllabus vs "Quiz #2 Wed. June 10" from an
    announcement), collapse to one. Containment (not exact match) is what catches the
    differently-worded duplicates chunked interpretation surfaces. Within a day, collisions
    between genuinely-distinct deadlines are very unlikely.

    Of a duplicate set we keep the best: lower `rank` (assignment/quiz beats calendar_event),
    then higher confidence, then the shorter (cleaner) title.
    """
    rank = {"assignment": 0, "quiz": 1, "exam": 1, "lab": 1, "project": 1,
            "calendar_event": 5, "other": 6}
    conf_rank = {"high": 0, "medium": 1, "low": 2}

    def pref(d: Deadline) -> tuple:
        # sort so the one we want to KEEP comes first within a day group
        return (rank.get(d.type, 6), conf_rank.get(d.confidence, 3),
                len(_norm_title(d.title)))

    by_day: dict[str, list[Deadline]] = {}
    for d in deadlines:
        by_day.setdefault((d.final_due_date or "")[:10], []).append(d)

    result: list[Deadline] = []
    for group in by_day.values():
        kept: list[Deadline] = []
        for d in sorted(group, key=pref):
            dk = _norm_title(d.title)
            if dk and any(
                _norm_title(k.title) in dk or dk in _norm_title(k.title)
                for k in kept
            ):
                continue  # duplicate of an already-kept (better) deadline this day
            kept.append(d)
        result.extend(kept)
    return result


def deadlines_to_dicts(deadlines: list[Deadline]) -> list[dict]:
    return [asdict(d) for d in deadlines]


def build_interpreter(
    backend: str | None = None,
    url: str | None = None,
    model: str | None = None,
    stream: bool = False,
) -> Interpreter:
    """Pick an interpreter backend from explicit args / env (`LLM_BACKEND`).

    Shared by the `interpret` CLI and the ingestion backend so both resolve the model
    the same way. 'ollama' uses the native /api/chat (thinking off on Qwen3.x); anything
    else uses the OpenAI-compatible endpoint.
    """
    backend = (backend or os.environ.get("LLM_BACKEND", "openai")).lower()
    if backend == "ollama":
        return OllamaInterpreter(base_url=url, model=model)
    return LocalInterpreter(base_url=url, model=model, stream=stream)


def interpret_courses(
    store, course_ids: list[int], interp: Interpreter
) -> list[Deadline]:
    """Interpret each course's items into deadlines. Per-course failures are logged and
    skipped (one bad course shouldn't sink the batch). No printing of model output — the
    caller decides how to report. Shared by the backend's /ingest pipeline."""
    import sys

    items_by_course = store.get_items_for_courses(course_ids)
    out: list[Deadline] = []
    for oid in course_ids:
        items = items_by_course.get(oid, [])
        if not items:
            continue
        try:
            out.extend(
                interp.interpret_course(oid, store.course_name(oid) or str(oid), items)
            )
        except Exception as exc:
            print(f"  ! interpret failed for {oid}: {type(exc).__name__}: {exc}",
                  file=sys.stderr)
    return out


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
    store = Store(cfg.database_url, cfg.institution)
    ids = _target_courses(store, args)
    items_by_course = store.get_items_for_courses(ids)
    print(f"Interpreting {len(ids)} course(s)...")

    if args.dry_run:
        total = 0
        total_chunks = 0
        for oid in ids:
            items = items_by_course.get(oid, [])
            ocr_pass(items)  # so token sizing reflects OCR'd text too
            chunks = render_chunks(store.course_name(oid) or str(oid), items)
            chars = sum(len(c) for c in chunks)
            total += chars
            total_chunks += len(chunks)
            print(f"  [{oid}] {store.course_name(oid) or '':40.40} "
                  f"items={len(items):3} chunks={len(chunks):2} {chars:6} chars "
                  f"(~{chars//4} tok)")
        print(f"\nDRY RUN — no model called. {total_chunks} chunk(s) / "
              f"~{total//4} tokens across courses (1 model call per chunk).")
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
