import pytest

from thesis.models import Exchange
from thesis.symbols import AmbiguousSymbolError, InvalidSymbolError, normalize_symbol


@pytest.mark.parametrize(
    ("raw", "exchange", "code", "instrument_id"),
    [
        ("sh600519", Exchange.SH, "600519", "CN.SH.600519"),
        ("SH600519", Exchange.SH, "600519", "CN.SH.600519"),
        ("600519", Exchange.SH, "600519", "CN.SH.600519"),
        ("sz300750", Exchange.SZ, "300750", "CN.SZ.300750"),
        ("300750", Exchange.SZ, "300750", "CN.SZ.300750"),
        ("002437", Exchange.SZ, "002437", "CN.SZ.002437"),
        ("CN.SZ.002437", Exchange.SZ, "002437", "CN.SZ.002437"),
        ("cn.sh.600519", Exchange.SH, "600519", "CN.SH.600519"),
    ],
)
def test_normalize_supported_stock_symbols(raw, exchange, code, instrument_id):
    instrument = normalize_symbol(raw)
    assert instrument.exchange is exchange
    assert instrument.code == code
    assert instrument.instrument_id == instrument_id


@pytest.mark.parametrize(
    "raw",
    ["", "60051", "6005190", "hk00700", "600ABC", "sh300750", "CN.SH.300750", "US.NYSE.BABA"],
)
def test_invalid_stock_symbols_fail_explicitly(raw):
    with pytest.raises(InvalidSymbolError):
        normalize_symbol(raw)


@pytest.mark.parametrize("raw", ["123456", "510300", "999999"])
def test_unrecognized_six_digit_namespace_is_not_guessed(raw):
    with pytest.raises(AmbiguousSymbolError, match="cannot reliably infer exchange"):
        normalize_symbol(raw)
