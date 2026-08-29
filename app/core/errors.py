from fastapi import HTTPException, status

# Shared OpenAPI response descriptions, so the docs stay uniform too.
NOT_FOUND = {404: {"description": "Resource does not exist"}}
CONFLICT = {409: {"description": "Conflicts with current state"}}
BAD_GATEWAY = {502: {"description": "An upstream provider refused the request"}}


def not_found(resource: str, identifier: object) -> HTTPException:
    return HTTPException(
        status.HTTP_404_NOT_FOUND, f"No {resource} with id {identifier}"
    )


def conflict(detail: str) -> HTTPException:
    return HTTPException(status.HTTP_409_CONFLICT, detail)


def unprocessable(detail: str) -> HTTPException:
    """422 for requests that parse correctly but violate a business rule —
    a closed restaurant, an out-of-range address, a subtotal under the minimum.
    """
    return HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail)


def bad_gateway(detail: str) -> HTTPException:
    """502 when an upstream provider failed — not the caller's fault.

    StorageError's own docstring says "Surface as 502, never as 500", and a 422
    is worse than either: it tells the client its request was invalid, so it
    retries with changes forever instead of backing off while the provider is
    down. The detail must be a fixed sentence, never the provider's own body,
    which carries bucket names and request ids.
    """
    return HTTPException(status.HTTP_502_BAD_GATEWAY, detail)
