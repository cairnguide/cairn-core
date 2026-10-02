"""Error responses in RFC 9457 problem details form (application/problem+json).

Nothing here echoes user input. Database messages are never passed through
because their detail text can contain the failing document (a validation
error's errInfo lists the values it considered), which would put names and
dates of birth into a response or a log.
"""
from __future__ import annotations

import logging

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


class RuleViolation(Exception):
    """A rule the data layer (store.py) enforces, in place of a PostgreSQL policy, function, or trigger.

    kind is one of:
      insufficient_privilege  not a member of the case, or the account can't make this change
      prerequisite            an earlier step isn't done (onboarding order, a journey already started)
      invalid_parameter       a value the rule doesn't know (an unknown step, template, or mode)
      check_violation         a value outside its allowed shape. constraint names the rule.
    """

    def __init__(self, kind: str, constraint: str | None = None):
        super().__init__(kind)
        self.kind = kind
        self.constraint = constraint


# Plain-language messages for named rules the user can actually trip.
CONSTRAINT_MESSAGES = {
    "death_not_before_birth":
        "The date of death is earlier than the date of birth. Please check both dates.",
    "deceased_date_of_birth_check": "The date of birth can't be in the future.",
    "deceased_date_of_death_check": "The date of death can't be in the future.",
    "state_code_check": "Please choose a state from the list.",
}

# MongoDB server error codes. https://www.mongodb.com/docs/manual/reference/error-codes/
UNAUTHORIZED = 13
WRITE_CONFLICT = 112
DOCUMENT_VALIDATION_FAILURE = 121


def map_db_error(exc: Exception) -> ApiError:
    from pymongo.errors import DuplicateKeyError, OperationFailure, PyMongoError

    if isinstance(exc, RuleViolation):
        # Log the rule only. Never a value.
        log.warning("data rule refused kind=%s constraint=%s", exc.kind, exc.constraint)
        if exc.kind == "insufficient_privilege":
            return case_access_denied()
        if exc.kind == "prerequisite":
            return ApiError(409, "out_of_order", "There's an earlier step to finish first.")
        if exc.kind == "check_violation" and exc.constraint in CONSTRAINT_MESSAGES:
            return ApiError(422, "invalid_value", CONSTRAINT_MESSAGES[exc.constraint], constraint=exc.constraint)
        return ApiError(422, "invalid_value", "One of the values wasn't accepted. Please check and try again.")

    code = getattr(exc, "code", None)
    labels = [label for label in ("TransientTransactionError", "UnknownTransactionCommitResult")
              if isinstance(exc, PyMongoError) and exc.has_error_label(label)]
    # Log the error class and code only. Never the message or errInfo.
    log.warning("database error type=%s code=%s labels=%s", type(exc).__name__, code, ",".join(labels))
    if isinstance(exc, DuplicateKeyError):
        return ApiError(409, "already_exists", "This record already exists.")
    if code == DOCUMENT_VALIDATION_FAILURE:
        return ApiError(422, "invalid_value", "One of the values wasn't accepted. Please check and try again.")
    if code == WRITE_CONFLICT or labels:
        return ApiError(409, "try_again", "Something else changed this at the same moment. Please try again.")
    if isinstance(exc, OperationFailure) and code == UNAUTHORIZED:
        return case_access_denied()
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
