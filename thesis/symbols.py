from __future__ import annotations

import re

from .models import Exchange, InstrumentRef


class InvalidSymbolError(ValueError):
    pass


class AmbiguousSymbolError(InvalidSymbolError):
    pass


_PREFIXED = re.compile(r"^(?P<exchange>sh|sz|bj)(?P<code>\d{6})$", re.IGNORECASE)
_INSTRUMENT_ID = re.compile(r"^cn\.(?P<exchange>sh|sz|bj)\.(?P<code>\d{6})$", re.IGNORECASE)
_SIX_DIGITS = re.compile(r"^\d{6}$")

# MVP is stock-only. These prefixes intentionally do not infer exchange for
# funds, bonds or indices whose six-digit namespaces overlap with securities.
_STOCK_PREFIXES: dict[Exchange, tuple[str, ...]] = {
    Exchange.SH: ("600", "601", "603", "605", "688", "689"),
    Exchange.SZ: ("000", "001", "002", "003", "300", "301"),
    Exchange.BJ: ("430", "830", "831", "832", "833", "834", "835", "836", "837", "838", "839", "870", "871", "872", "873", "920"),
}


def _infer_exchange(code: str) -> Exchange:
    matches = [exchange for exchange, prefixes in _STOCK_PREFIXES.items() if code.startswith(prefixes)]
    if len(matches) != 1:
        raise AmbiguousSymbolError(
            f"cannot reliably infer exchange for six-digit stock code {code!r}; "
            "use an explicit sh/sz/bj prefix"
        )
    return matches[0]


def normalize_symbol(symbol: str, *, name: str | None = None) -> InstrumentRef:
    """Normalize a supported A-share symbol to an InstrumentRef.

    Six-digit codes are inferred only for known stock namespaces. Explicit
    prefixes are validated against those namespaces to catch mismatches.
    """

    if not isinstance(symbol, str):
        raise InvalidSymbolError("symbol must be a string")
    candidate = symbol.strip().lower()
    match = _PREFIXED.fullmatch(candidate) or _INSTRUMENT_ID.fullmatch(candidate)
    if match:
        code = match.group("code")
        exchange = Exchange(match.group("exchange").upper())
        inferred = _infer_exchange(code)
        if inferred is not exchange:
            raise InvalidSymbolError(
                f"symbol prefix {exchange.value.lower()} conflicts with stock code {code} "
                f"(recognized as {inferred.value})"
            )
        return InstrumentRef(exchange=exchange, code=code, name=name)

    if _SIX_DIGITS.fullmatch(candidate):
        return InstrumentRef(exchange=_infer_exchange(candidate), code=candidate, name=name)

    raise InvalidSymbolError(
        f"invalid A-share symbol {symbol!r}; expected CN.SH.600519, sh600519, sz300750, "
        "bj920xxx, or a supported six-digit stock code"
    )
