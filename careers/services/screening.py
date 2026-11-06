"""Yes/No screening questions of a posting (``careers_job_position.screening_questions``) and their answers.

A posting carries an ordered list of ``{key, question, required}``; the website renders each as a Yes/No choice and
sends the answers back as ``{key: true|false}``. The application stores ``[{key, question, answer}]`` — the question
text is copied, so editing or removing a question later never changes what a candidate was asked.

``key`` is the stable id of a question: the Studio sends it back unchanged on edit; a new question gets one here.
"""

from __future__ import annotations

import re
import uuid

from core.errors import DomainError

MAX_QUESTIONS = 10
QUESTION_MAX_LENGTH = 300
KEY_RE = re.compile(r"^[a-z0-9_-]{1,40}$")
YES = ("true", "yes", "1")
NO = ("false", "no", "0")


def _invalid(message: str) -> DomainError:
    return DomainError("validation_error", message, errors={"screening_questions": [message]})


def clean_questions(raw) -> list[dict]:
    """The validated, normalised question list (keys assigned to new questions)."""
    if raw in (None, ""):
        return []
    if not isinstance(raw, list):
        raise _invalid("Send a list of questions.")
    if len(raw) > MAX_QUESTIONS:
        raise _invalid(f"At most {MAX_QUESTIONS} screening questions.")
    cleaned, seen = [], set()
    for item in raw:
        if not isinstance(item, dict):
            raise _invalid("Each question needs `question` text.")
        text = str(item.get("question") or "").strip()
        if not text:
            raise _invalid("A question cannot be empty.")
        if len(text) > QUESTION_MAX_LENGTH:
            raise _invalid(f"Keep each question under {QUESTION_MAX_LENGTH} characters.")
        key = str(item.get("key") or "").strip() or f"q{uuid.uuid4().hex[:8]}"
        if not KEY_RE.match(key):
            raise _invalid("A question key may use lower-case letters, digits, '-' and '_' (at most 40).")
        if key in seen:
            raise _invalid(f"Two questions share the key '{key}'.")
        seen.add(key)
        cleaned.append({"key": key, "question": text, "required": bool(item.get("required", True))})
    return cleaned


def _as_bool(value) -> bool | None:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in YES:
        return True
    if text in NO:
        return False
    return None


def answers_for(questions: list[dict], raw: dict) -> list[dict]:
    """``[{key, question, answer}]`` for ``questions`` from the submitted ``{key: yes/no}`` (unknown keys ignored).

    A required question without a Yes/No answer is a 400 naming the question.
    """
    raw = raw or {}
    answers, missing = [], []
    for question in questions or []:
        answer = _as_bool(raw[question["key"]]) if raw.get(question["key"]) not in (None, "") else None
        if answer is None:
            if question.get("required", True):
                missing.append(question["question"])
            continue
        answers.append({"key": question["key"], "question": question["question"], "answer": answer})
    if missing:
        raise DomainError("validation_error", "Please answer every required question.", errors={"screening_answers": [f"Please answer: {text}" for text in missing]})
    return answers
