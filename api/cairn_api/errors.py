"""Error responses in RFC 9457 problem details form (application/problem+json).

Nothing here echoes user input. Database messages are never passed through
because their detail text can contain the failing row, which would put names
and dates of birth into a response or a log.
"""
from __future__ import annotations

import logging

import psycopg
from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

log = logging.getLogger("cairn_api")

PROBLEM_JSON = "application/problem+json"


class ApiError(Exception):
    def __init__(self, status: int, code: str, detail: str, **extra):
        super().__init__(code)
        self.status = status
        self.code = code
        self.detail = detail
        self.extra = extra


def case_access_denied() -> ApiError:
    # One response for "not a member" and "does not exist" so the API does not
    # reveal whether a case id is real.
    return ApiError(403, "case_access_denied",
                    "You don't have access to this case, or it doesn't exist.")


# Plain-language messages for named constraints the user can actually trip.
CONSTRAINT_MESSAGES = {
    "death_not_before_birth":
        "The date of death is earlier than the date of birth. Please check both dates.",
    "deceased_date_of_birth_check": "The date of birth can't be in the future.",
    "deceased_date_of_death_check": "The date of death can't be in the future.",
    "state_code_check": "Please choose a state from the list.",
}


def map_db_error(exc: psycopg.Error) -> ApiError:
    state = exc.sqlstate or ""
    constraint = getattr(exc.diag, "constraint_name", None)
    # Log the error class and constraint only. Never the message or detail.
    log.warning("database error sqlstate=%s constraint=%s", state, constraint)

    if state == "23514":  # check_violation, including domain checks
        if constraint in CONSTRAINT_MESSAGES:
            return ApiError(422, "invalid_value", CONSTRAINT_MESSAGES[constraint], constraint=constraint)
        if constraint == "tri_state_check":
            # The client can only send yes, no, or unknown. Treat this as a client bug.
            return ApiError(400, "client_bug", "The app sent an answer the server doesn't accept.")
        return ApiError(422, "invalid_value", "One of the values wasn't accepted. Please check and try again.")
    if state == "23505":
        return ApiError(409, "already_exists", "This record already exists.")
    if state == "23502":
        return ApiError(422, "missing_value", "A required answer is missing.")
    if state == "55000":  # object_not_in_prerequisite_state, from cairn.advance_onboarding
        return ApiError(409, "out_of_order", "There's an earlier step to finish first.")
    if state == "42501":  # insufficient_privilege, including row-level security rejections
        return case_access_denied()
    if state.startswith("22"):
        return ApiError(422, "invalid_value", "One of the values wasn't in a format we could read.")
    return ApiError(500, "internal_error", "Something went wrong on our side. Your information was not changed.")


def _problem(status: int, code: str, detail: str, **extra) -> JSONResponse:
    body = {"type": f"https://cairn.invalid/problems/{code}", "title": code.replace("_", " ").capitalize(),
            "status": status, "code": code, "detail": detail}
    body.update(extra)
    return JSONResponse(body, status_code=status, media_type=PROBLEM_JSON)


async def api_error_handler(_: Request, exc: ApiError) -> JSONResponse:
    return _problem(exc.status, exc.code, exc.detail, **exc.extra)


async def validation_error_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
    # FastAPI's default 422 body includes the submitted value ("input"). Strip it
    # so sensitive fields are not reflected back or captured by proxies.
    errors = [
        {"field": ".".join(str(p) for p in e.get("loc", ()) if p != "body"),
         "message": e.get("msg", "Invalid value"), "type": e.get("type", "value_error")}
        for e in exc.errors()
    ]
    return _problem(422, "validation_failed", "Some answers need another look.", errors=errors)


async def unhandled_error_handler(_: Request, exc: Exception) -> JSONResponse:
    log.error("unhandled error type=%s", type(exc).__name__)
    return _problem(500, "internal_error", "Something went wrong on our side. Your information was not changed.")
