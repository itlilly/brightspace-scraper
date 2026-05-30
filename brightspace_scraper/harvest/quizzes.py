"""Harvest quizzes for a course."""

from __future__ import annotations

from ..client import BrightspaceClient
from ..models import QUIZ, HarvestItem
from ..util import html_to_text, item_id


def harvest_quizzes(client: BrightspaceClient, org_unit_id: int) -> list[HarvestItem]:
    quizzes = client.get_paged(f"/d2l/api/le/{client.le}/{org_unit_id}/quizzes/")
    items: list[HarvestItem] = []
    for q in quizzes or []:
        qid = q.get("QuizId")
        if qid is None:
            continue
        due = q.get("DueDate") or q.get("EndDate")
        # html_to_text handles the nested RichText block {Text: {Html, Text}} directly.
        body = html_to_text(q.get("Instructions")) or html_to_text(q.get("Description"))
        items.append(
            HarvestItem(
                id=item_id(QUIZ, org_unit_id, qid),
                org_unit_id=org_unit_id,
                type=QUIZ,
                title=q.get("Name") or f"Quiz {qid}",
                structured_due_date=due,
                body_text=body,
                source_url=(
                    f"{client.base}/d2l/lms/quizzing/user/quiz_summary.d2l"
                    f"?qi={qid}&ou={org_unit_id}"
                ),
                raw={"quiz": q},
            )
        )
    return items
