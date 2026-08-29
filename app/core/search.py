"""One LIKE-pattern escaper, instead of four copies and two omissions.

This helper existed byte-for-byte identically in four routers --
`catalog._contains`, `users._contains`, `admin_operations` and `admin_finance` --
and the duplication was acknowledged in their comments ("the same helper,
character for character, as `_contains()` in app/routers/users.py").

Two call sites in `app/repositories/catalog.py` were missed, and
`app/routers/catalog.py:115` even records that the repository clause
"interpolates the term into the pattern unescaped, and both had to change" --
only one of them did.

Unescaped is not SQL injection: the value is a bound parameter either way. It is
a filter BYPASS and a scan amplifier. `?q=%` becomes the pattern `%%%`, which
matches every row, so a search returns the whole table regardless of the term and
a `city=` filter silently matches every city. `?q=%a%b%c%d%e%f%g%` forces a
pathological scan over an unindexed text column, on an unauthenticated route.
"""

#: Backslash, because that is what `.ilike(..., escape="\\")` is told to expect.
ESCAPE_CHARACTER = "\\"

#: The three characters LIKE treats as syntax rather than as text. Backslash
#: first: escaping it after the others would double-escape their new prefixes.
_SPECIAL = (ESCAPE_CHARACTER, "%", "_")


def escape_like(term: str) -> str:
    """Neutralise LIKE syntax so a term matches itself and nothing more."""
    for character in _SPECIAL:
        term = term.replace(character, ESCAPE_CHARACTER + character)
    return term


def contains(term: str) -> str:
    """A `%term%` pattern in which `term` can only ever match itself.

    Pair with `column.ilike(contains(term), escape="\\\\")` -- the escape
    argument is not optional, because without it Postgres has no idea that the
    backslashes this function inserted are escapes.
    """
    return f"%{escape_like(term)}%"
