#!/usr/bin/env python3
"""무료 일봉 주식 스크리너 데이터 갱신기.

API 키 없이 공개 웹 목록 + yfinance 일봉을 사용한다.
개인 연구용이며 주문 전 증권사 데이터 확인이 필요하다.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd
import requests
import yfinance as yf
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "docs" / "data"
USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) "
    "AppleWebKit/605.1.15 Version/18.0 Mobile/15E148 Safari/604.1"
)
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.7"})


@dataclass(frozen=True)
class Stock:
    ticker: str
    name: str
    exchange: str
    market: str
    indexes: tuple[str, ...] = ()
    preferred: bool = False
    spac: bool = False
    asset_type: str = "stock"
    leveraged: bool = False
    inverse: bool = False
    size_value: float | None = None


def log(message: str) -> None:
    print(message, flush=True)


def get_text(url: str, retries: int = 3, timeout: int = 30) -> str:
    last: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            response = SESSION.get(url, timeout=timeout)
            response.raise_for_status()
            response.encoding = response.apparent_encoding or response.encoding
            return response.text
        except Exception as exc:  # network retry
            last = exc
            log(f"요청 실패 ({attempt}/{retries}): {url} / {exc}")
            time.sleep(attempt * 2)
    raise RuntimeError(f"페이지를 가져오지 못했습니다: {url}") from last


def is_preferred_kr(name: str) -> bool:
    return bool(re.search(r"(우|우B|우C|우선주|\d우)$", name.replace(" ", "")))


def get_json(url: str, retries: int = 3, timeout: int = 30) -> object:
    """로그인 없이 공개된 읽기 전용 JSON을 가져온다."""
    last: Exception | None = None
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://stock.naver.com/",
    }
    for attempt in range(1, retries + 1):
        try:
            response = SESSION.get(url, timeout=timeout, headers=headers)
            response.raise_for_status()
            return response.json()
        except Exception as exc:
            last = exc
            log(f"JSON 요청 실패 ({attempt}/{retries}): {url} / {exc}")
            time.sleep(attempt * 2)
    raise RuntimeError(f"JSON을 가져오지 못했습니다: {url}") from last


def _stock_rows(payload: object) -> list[dict[str, object]]:
    """응답 구조가 조금 바뀌어도 종목코드가 든 행을 재귀적으로 찾는다."""
    rows: list[dict[str, object]] = []

    def walk(value: object) -> None:
        if isinstance(value, dict):
            code = next(
                (
                    value.get(key)
                    for key in ("itemCode", "stockCode", "symbolCode", "code")
                    if value.get(key) is not None
                ),
                None,
            )
            code_text = re.sub(r"\D", "", str(code or ""))
            if len(code_text) == 6:
                rows.append(value)
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(payload)
    return rows


def _row_name(row: dict[str, object], code: str) -> str:
    for key in ("stockName", "itemName", "korName", "name"):
        value = row.get(key)
        if value and str(value).strip():
            return str(value).strip()
    return code



def _numeric_value(value: object) -> float | None:
    """숫자/문자/네이버 {rawValue: ...} 형태를 가능한 범위에서 숫자로 변환."""
    if value is None:
        return None
    if isinstance(value, dict):
        for key in ("rawValue", "value", "amount", "marketValue", "marketSum", "marketCap"):
            if key in value:
                parsed = _numeric_value(value.get(key))
                if parsed is not None:
                    return parsed
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return number if math.isfinite(number) and number > 0 else None
    text = str(value).strip().replace(",", "")
    if not text or text.lower() in ("nan", "none", "-"):
        return None
    # '123.4조', '5,600억' 같은 표기도 처리
    m = re.search(r"(-?[0-9]+(?:\.[0-9]+)?)\s*(조|억|만)?", text)
    if not m:
        return None
    number = float(m.group(1))
    unit = m.group(2)
    if unit == "조": number *= 1e12
    elif unit == "억": number *= 1e8
    elif unit == "만": number *= 1e4
    return number if math.isfinite(number) and number > 0 else None


def _extract_kr_market_cap(row: dict[str, object]) -> float | None:
    """네이버 종목 목록 행에서 시가총액(원)을 찾는다. 필드명이 바뀌어도 후보를 넓게 본다."""
    preferred_keys = (
        "marketValue", "marketSum", "marketCap", "marketCapitalization",
        "marketValueRaw", "marketSumRaw", "marketCapRaw",
    )
    for key in preferred_keys:
        if key in row:
            parsed = _numeric_value(row.get(key))
            if parsed is not None:
                return parsed
    # summary 같은 중첩 객체 안도 확인
    for key, value in row.items():
        key_l = str(key).lower()
        if any(token in key_l for token in ("marketvalue", "marketsum", "marketcap")):
            parsed = _numeric_value(value)
            if parsed is not None:
                return parsed
    return None

def get_kr_listing_naver() -> list[Stock]:
    """새 네이버증권의 공개 읽기 전용 목록 API를 사용한다."""
    stocks: list[Stock] = []
    page_size = 100
    for market_type, exchange, suffix in (
        ("KOSPI", "KOSPI", "KS"),
        ("KOSDAQ", "KOSDAQ", "KQ"),
    ):
        seen: set[str] = set()
        for start_idx in range(0, 5000, page_size):
            url = (
                "https://stock.naver.com/api/domestic/market/stock/default"
                f"?tradeType=KRX&marketType={market_type}&orderType=marketSum"
                f"&startIdx={start_idx}&pageSize={page_size}"
            )
            payload = get_json(url)
            rows = _stock_rows(payload)
            added = 0
            for row in rows:
                raw_code = next(
                    (
                        row.get(key)
                        for key in ("itemCode", "stockCode", "symbolCode", "code")
                        if row.get(key) is not None
                    ),
                    "",
                )
                code = re.sub(r"\D", "", str(raw_code))
                if len(code) != 6 or code in seen:
                    continue
                seen.add(code)
                name = _row_name(row, code)
                stocks.append(
                    Stock(
                        ticker=f"{code}.{suffix}",
                        name=name,
                        exchange=exchange,
                        market="KR",
                        preferred=is_preferred_kr(name),
                        spac="스팩" in name,
                        size_value=_extract_kr_market_cap(row),
                    )
                )
                added += 1
            log(f"{exchange} 목록 {start_idx // page_size + 1}페이지: {added}개")
            if added == 0:
                break
            time.sleep(0.2)
    return stocks


def get_kr_listing_kind() -> list[Stock]:
    """네이버 목록이 막힐 때 KRX KIND 상장법인 목록을 예비로 사용한다."""
    url = "https://kind.krx.co.kr/corpgeneral/corpList.do?method=download&searchType=13"
    html = get_text(url, retries=3, timeout=60)
    tables = pd.read_html(StringIO(html), header=0)
    if not tables:
        return []
    df = normalize_columns(max(tables, key=len))
    columns = {str(c).replace(" ", "").strip(): c for c in df.columns}
    code_col = next((columns[x] for x in ("종목코드", "단축코드") if x in columns), None)
    name_col = next((columns[x] for x in ("회사명", "종목명") if x in columns), None)
    market_col = next((columns[x] for x in ("시장구분", "시장") if x in columns), None)
    if code_col is None or name_col is None or market_col is None:
        raise RuntimeError("KRX KIND 목록에서 필요한 열을 찾지 못했습니다.")

    stocks: list[Stock] = []
    seen: set[str] = set()
    for _, row in df.iterrows():
        code = re.sub(r"\D", "", str(row[code_col])).zfill(6)[-6:]
        name = str(row[name_col]).strip()
        market_text = str(row[market_col]).strip()
        if len(code) != 6 or code in seen or not name or name.lower() == "nan":
            continue
        if "코스닥" in market_text:
            exchange, suffix = "KOSDAQ", "KQ"
        elif "유가" in market_text or "코스피" in market_text:
            exchange, suffix = "KOSPI", "KS"
        else:
            continue
        seen.add(code)
        stocks.append(
            Stock(
                ticker=f"{code}.{suffix}",
                name=name,
                exchange=exchange,
                market="KR",
                preferred=is_preferred_kr(name),
                spac="스팩" in name,
            )
        )
    log(f"KRX KIND 예비 목록: {len(stocks)}개")
    return stocks


def get_kr_listing() -> list[Stock]:
    """한국 종목 목록. 새 네이버 API → KRX KIND 순서로 시도한다."""
    stocks: list[Stock] = []
    try:
        stocks = get_kr_listing_naver()
    except Exception as exc:
        log(f"네이버 새 목록 API 실패: {exc}")

    if len(stocks) < 500:
        log(f"네이버 목록이 {len(stocks)}개뿐이라 KRX KIND 예비 목록을 사용합니다.")
        try:
            stocks = get_kr_listing_kind()
        except Exception as exc:
            log(f"KRX KIND 예비 목록 실패: {exc}")

    if len(stocks) < 500:
        raise RuntimeError(f"한국 종목 목록이 지나치게 적습니다: {len(stocks)}개")
    return stocks


def normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out.columns = [str(c).strip() if not isinstance(c, tuple) else " ".join(map(str, c)).strip() for c in out.columns]
    return out


def find_table(tables: list[pd.DataFrame], symbol_names: Iterable[str], company_names: Iterable[str], minimum: int) -> tuple[pd.DataFrame, str, str]:
    for table in tables:
        table = normalize_columns(table)
        columns = {str(c).strip(): c for c in table.columns}
        sym = next((columns[x] for x in symbol_names if x in columns), None)
        comp = next((columns[x] for x in company_names if x in columns), None)
        if sym is not None and comp is not None and len(table) >= minimum:
            return table, str(sym), str(comp)
    raise RuntimeError("필요한 종목 목록 표를 찾지 못했습니다.")


def read_html_tables(url: str) -> list[pd.DataFrame]:
    html = get_text(url)
    return pd.read_html(StringIO(html))


def get_sp500() -> list[Stock]:
    sources = [
        "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
        "https://datahub.io/core/s-and-p-500-companies/r/constituents.csv",
    ]
    last: Exception | None = None
    for url in sources:
        try:
            if url.endswith(".csv"):
                df = pd.read_csv(StringIO(get_text(url)))
                symbol_col = "Symbol"
                name_col = "Name" if "Name" in df.columns else "Security"
            else:
                df, symbol_col, name_col = find_table(
                    read_html_tables(url),
                    ("Symbol", "Ticker"),
                    ("Security", "Company", "Name"),
                    450,
                )
            result = []
            for _, row in df.iterrows():
                raw = str(row[symbol_col]).strip()
                if not raw or raw.lower() == "nan":
                    continue
                ticker = raw.replace(".", "-")
                result.append(Stock(ticker, str(row[name_col]).strip(), "US", "US", ("S&P500",)))
            if len(result) >= 450:
                return result
        except Exception as exc:
            last = exc
            log(f"S&P500 목록 출처 실패: {url} / {exc}")
    raise RuntimeError("S&P500 목록을 가져오지 못했습니다.") from last


NASDAQ100_FALLBACK = """
ADBE AMD ABNB GOOGL GOOG AMZN AEP AMGN ADI ANSS AAPL AMAT APP ARM ASML AZN TEAM ADSK ADP AXON BKR BIIB BKNG AVGO CDNS CDW CHTR CCEP CSCO CSGP COST CRWD CSX DDOG DXCM FANG DASH EA EXC FAST FTNT GEHC GILD GFS HON IDXX INTC INTU ISRG KDP KLAC KHC LRCX LIN MAR MRVL MELI META MCHP MU MSFT MRNA MDLZ MDB MNST NFLX NVDA NXPI ORLY ODFL ON PCAR PLTR PANW PAYX PYPL PDD PEP QCOM REGN ROP ROST SBUX SNPS TTWO TMUS TSLA TXN TTD VRSK VRTX WBD WDAY XEL ZS
""".split()


def get_nasdaq100() -> list[Stock]:
    try:
        tables = read_html_tables("https://en.wikipedia.org/wiki/Nasdaq-100")
        df, symbol_col, name_col = find_table(
            tables,
            ("Ticker", "Symbol"),
            ("Company", "Security", "Name"),
            90,
        )
        result = []
        for _, row in df.iterrows():
            raw = str(row[symbol_col]).strip()
            if not raw or raw.lower() == "nan":
                continue
            result.append(Stock(raw.replace(".", "-"), str(row[name_col]).strip(), "US", "US", ("NASDAQ100",)))
        if len(result) >= 90:
            return result
    except Exception as exc:
        log(f"NASDAQ100 목록 자동 갱신 실패, 내장 예비 목록 사용: {exc}")
    return [Stock(t, t, "US", "US", ("NASDAQ100",)) for t in NASDAQ100_FALLBACK]


def _parse_pipe_file(url: str) -> pd.DataFrame:
    text = get_text(url, retries=4, timeout=60)
    lines = [line for line in text.splitlines() if line and not line.startswith("File Creation Time")]
    return pd.read_csv(StringIO("\n".join(lines)), sep="|")


def _is_excluded_us_security(name: str, symbol: str) -> bool:
    text = f"{name} {symbol}".upper()
    blocked = (
        " WARRANT", " WARRANTS", " WTS", " UNIT", " UNITS", " RIGHT", " RIGHTS",
        " PREFERRED", " PFD", " DEBENTURE", " NOTE DUE", " BOND", " BENEFICIAL INTEREST",
    )
    if any(word in text for word in blocked):
        return True
    # Nasdaq 특수기호: ^ 우선주, + 권리, = 유닛 등
    if any(ch in symbol for ch in ("^", "+", "=")):
        return True
    return False


def _etf_flags(name: str) -> tuple[bool, bool]:
    text = name.upper()
    leveraged_words = ("2X", "3X", "ULTRA", "BULL 2", "BULL 3", "LEVERAGED")
    inverse_words = ("INVERSE", "SHORT", "BEAR", "-1X", "-2X", "-3X")
    return any(w in text for w in leveraged_words), any(w in text for w in inverse_words)


def get_us_full_listing() -> list[Stock]:
    """Nasdaq Trader 공식 심볼 목록에서 미국 일반주와 ETF 전체를 만든다."""
    urls = (
        "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt",
        "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt",
    )
    nasdaq = _parse_pipe_file(urls[0])
    other = _parse_pipe_file(urls[1])
    index_members: dict[str, set[str]] = {}
    try:
        for stock in get_sp500() + get_nasdaq100():
            index_members.setdefault(stock.ticker, set()).update(stock.indexes)
    except Exception as exc:
        log(f"지수 구성종목 표시는 생략합니다: {exc}")

    stocks: dict[str, Stock] = {}

    def add(symbol: object, name: object, exchange: str, etf_value: object, test_value: object = "N", status_value: object = "N") -> None:
        raw = str(symbol).strip()
        sec_name = str(name).strip()
        if not raw or raw.lower() == "nan" or raw == "Symbol":
            return
        if str(test_value).strip().upper() == "Y":
            return
        # 금융상태가 정상(N) 아닌 종목은 데이터 오류 가능성이 높아 제외
        status = str(status_value).strip().upper()
        if status not in ("", "N", "NAN"):
            return
        ticker = raw.replace(".", "-")
        is_etf = str(etf_value).strip().upper() == "Y"
        if not is_etf and _is_excluded_us_security(sec_name, raw):
            return
        leveraged, inverse = _etf_flags(sec_name) if is_etf else (False, False)
        indexes = tuple(sorted(index_members.get(ticker, set())))
        stocks[ticker] = Stock(
            ticker=ticker,
            name=sec_name or ticker,
            exchange=exchange,
            market="US",
            indexes=indexes,
            asset_type="etf" if is_etf else "stock",
            leveraged=leveraged,
            inverse=inverse,
        )

    for _, row in nasdaq.iterrows():
        add(
            row.get("Symbol"), row.get("Security Name"), "NASDAQ", row.get("ETF"),
            row.get("Test Issue"), row.get("Financial Status"),
        )

    exchange_map = {
        "A": "NYSE American", "N": "NYSE", "P": "NYSE Arca",
        "Z": "Cboe BZX", "V": "IEX", "Q": "NASDAQ",
    }
    for _, row in other.iterrows():
        code = str(row.get("Exchange", "")).strip().upper()
        add(
            row.get("ACT Symbol"), row.get("Security Name"), exchange_map.get(code, code or "US"),
            row.get("ETF"), row.get("Test Issue"), "N",
        )

    if len(stocks) < 3000:
        raise RuntimeError(f"미국 전체 종목 목록이 지나치게 적습니다: {len(stocks)}개")
    etfs = sum(1 for x in stocks.values() if x.asset_type == "etf")
    log(f"미국 전체 목록: {len(stocks)}개 (ETF {etfs}개 포함)")
    return sorted(stocks.values(), key=lambda x: x.ticker)



def _screen_quotes(response: object) -> list[dict[str, object]]:
    if not isinstance(response, dict):
        return []
    for key in ("quotes", "results"):
        value = response.get(key)
        if isinstance(value, list):
            return [x for x in value if isinstance(x, dict)]
    finance = response.get("finance")
    if isinstance(finance, dict):
        result = finance.get("result")
        if isinstance(result, list) and result:
            item = result[0]
            if isinstance(item, dict) and isinstance(item.get("quotes"), list):
                return [x for x in item["quotes"] if isinstance(x, dict)]
    return []


def _quote_symbol(q: dict[str, object]) -> str:
    for key in ("symbol", "ticker"):
        value = q.get(key)
        if value:
            return str(value).strip().replace(".", "-")
    return ""


def _quote_size(q: dict[str, object], is_etf: bool) -> float | None:
    keys = (
        ("fundNetAssets", "fundnetassets", "netAssets", "netassets", "totalAssets", "totalassets", "marketCap", "intradaymarketcap")
        if is_etf else
        ("marketCap", "intradaymarketcap", "marketcap")
    )
    for key in keys:
        if key in q:
            parsed = _numeric_value(q.get(key))
            if parsed is not None:
                return parsed
    return None


def _flattened_row_value(row: pd.Series, wanted: tuple[str, ...]) -> float | None:
    """yfscreen DataFrame의 raw/fmt/대소문자 차이를 흡수해 숫자 필드를 찾는다."""
    normalized: dict[str, object] = {}
    for key, value in row.items():
        norm = re.sub(r"[^a-z0-9]", "", str(key).lower())
        normalized[norm] = value
    for key in wanted:
        base = re.sub(r"[^a-z0-9]", "", key.lower())
        for candidate in (base + "raw", base, base + "longfmt", base + "fmt"):
            if candidate in normalized:
                parsed = _numeric_value(normalized[candidate])
                if parsed is not None:
                    return parsed
    return None


def _yfscreen_symbol(row: pd.Series) -> str:
    for key in row.index:
        norm = re.sub(r"[^a-z0-9]", "", str(key).lower())
        if norm in ("symbol", "ticker"):
            value = row.get(key)
            if value:
                return str(value).strip().replace(".", "-")
    return ""


def get_us_size_map_yfscreen() -> dict[str, float]:
    """yfscreen으로 미국 일반주 시총과 ETF 순자산을 가져온다.

    yfinance의 screen pagination이 환경에 따라 비어버리는 문제를 피하기 위해
    Yahoo screener의 세션/crumb/POST/pagination을 전담하는 yfscreen을 1순위로 사용한다.
    """
    size_map: dict[str, float] = {}
    try:
        import yfscreen as yfs
    except Exception as exc:
        log(f"yfscreen을 불러오지 못했습니다: {exc}")
        return size_map

    for sec_type, label, is_etf in (("equity", "일반주", False), ("etf", "ETF", True)):
        try:
            query = yfs.create_query([["eq", ["region", "us"]]])
            payload = yfs.create_payload(sec_type, query)
            data = yfs.get_data(payload)
            if data is None:
                log(f"yfscreen {label}: 응답 없음")
                continue
            if not isinstance(data, pd.DataFrame):
                data = pd.DataFrame(data)
            if data.empty:
                log(f"yfscreen {label}: 0개")
                continue
            added = 0
            for _, row in data.iterrows():
                symbol = _yfscreen_symbol(row)
                if not symbol:
                    continue
                if is_etf:
                    value = _flattened_row_value(
                        row,
                        ("fundNetAssets", "fundnetassets", "netAssets", "netassets", "totalAssets", "totalassets", "intradaymarketcap", "marketCap"),
                    )
                else:
                    value = _flattened_row_value(row, ("intradaymarketcap", "marketCap", "marketcap"))
                if value is not None:
                    size_map[symbol] = value
                    added += 1
            log(f"yfscreen {label} 시총/규모: {added:,}개 확보 (조회 행 {len(data):,}개)")
        except Exception as exc:
            log(f"yfscreen {label} 시총/규모 조회 실패: {exc}")
    return size_map


def get_us_size_map_yfinance() -> dict[str, float]:
    """예비 경로: yfinance.screen으로 시총/ETF 순자산을 모은다."""
    size_map: dict[str, float] = {}
    try:
        from yfinance import EquityQuery, ETFQuery
    except Exception as exc:
        log(f"미국 시총 예비도구를 불러오지 못했습니다: {exc}")
        return size_map

    for query_cls, label, is_etf in ((EquityQuery, "일반주", False), (ETFQuery, "ETF", True)):
        try:
            query = query_cls("eq", ["region", "us"])
            offset = 0
            seen: set[str] = set()
            while offset < 20000:
                response = yf.screen(query, offset=offset, size=250, sortField="ticker", sortAsc=True)
                quotes = _screen_quotes(response)
                if not quotes:
                    break
                added = 0
                for q in quotes:
                    symbol = _quote_symbol(q)
                    if not symbol or symbol in seen:
                        continue
                    seen.add(symbol)
                    value = _quote_size(q, is_etf)
                    if value is not None:
                        size_map[symbol] = value
                    added += 1
                log(f"yfinance 예비 {label} {offset + 1}~{offset + len(quotes)} 조회")
                if len(quotes) < 250 or added == 0:
                    break
                offset += len(quotes)
                time.sleep(0.35)
        except Exception as exc:
            log(f"yfinance 예비 {label} 시총/규모 조회 실패: {exc}")
    return size_map


def get_us_size_map() -> dict[str, float]:
    primary = get_us_size_map_yfscreen()
    # 1,000개도 못 얻었으면 yfinance 예비 경로를 합친다. '전부 -' 상태를 조용히 통과시키지 않는다.
    if len(primary) < 1000:
        log(f"yfscreen 확보값이 {len(primary):,}개뿐이라 yfinance 예비 경로를 시도합니다.")
        fallback = get_us_size_map_yfinance()
        for symbol, value in fallback.items():
            primary.setdefault(symbol, value)
    log(f"미국 시총/ETF 순자산 최종 확보: {len(primary):,}개")
    return primary


def attach_us_sizes(stocks: list[Stock]) -> list[Stock]:
    size_map = get_us_size_map()
    stock_total = sum(1 for s in stocks if s.asset_type == "stock")
    etf_total = sum(1 for s in stocks if s.asset_type == "etf")
    stock_hit = sum(1 for s in stocks if s.asset_type == "stock" and s.ticker in size_map)
    etf_hit = sum(1 for s in stocks if s.asset_type == "etf" and s.ticker in size_map)
    log(f"시총 커버리지 일반주 {stock_hit:,}/{stock_total:,}, ETF {etf_hit:,}/{etf_total:,}")
    if stock_hit < 1000:
        raise RuntimeError(
            f"미국 일반주 시총 데이터가 너무 적습니다: {stock_hit:,}/{stock_total:,}. "
            "시총이 전부 '-'로 저장되는 것을 막기 위해 이번 갱신을 중단합니다."
        )
    return [replace(stock, size_value=size_map.get(stock.ticker)) for stock in stocks]

def get_us_listing() -> list[Stock]:
    return attach_us_sizes(get_us_full_listing())


def chunks(items: list[Stock], size: int) -> Iterable[list[Stock]]:
    for i in range(0, len(items), size):
        yield items[i:i + size]


def extract_history(downloaded: pd.DataFrame, ticker: str, batch_len: int) -> pd.DataFrame | None:
    if downloaded is None or downloaded.empty:
        return None
    frame: pd.DataFrame
    if isinstance(downloaded.columns, pd.MultiIndex):
        level0 = downloaded.columns.get_level_values(0)
        level1 = downloaded.columns.get_level_values(1)
        if ticker in level0:
            frame = downloaded[ticker].copy()
        elif ticker in level1:
            frame = downloaded.xs(ticker, axis=1, level=1).copy()
        else:
            return None
    elif batch_len == 1:
        frame = downloaded.copy()
    else:
        return None
    frame.columns = [str(c).title() for c in frame.columns]
    if "Close" not in frame.columns:
        return None
    frame = frame.dropna(subset=["Close"])
    return frame if not frame.empty else None


def finite(value: object, digits: int = 4) -> float | None:
    try:
        number = float(value)
        return round(number, digits) if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def trailing_return(close: pd.Series, periods: int) -> float | None:
    if len(close) <= periods:
        return None
    window = close.iloc[-periods - 1:]
    # 티커 재사용·합병 등으로 하루 가격이 비정상적으로 단절된 구간은 수익률을 숨긴다.
    daily = window.pct_change().replace([np.inf, -np.inf], np.nan).dropna()
    if not daily.empty and (daily.abs() > 0.70).any():
        return None
    base = window.iloc[0]
    value = finite((window.iloc[-1] / base - 1) * 100) if base else None
    if value is not None and abs(value) > 1000:
        return None
    return value


def compute(stock: Stock, history: pd.DataFrame) -> dict[str, object] | None:
    close = pd.to_numeric(history["Close"], errors="coerce").dropna()
    if len(close) < 25:
        return None
    high = pd.to_numeric(history.get("High", close), errors="coerce").reindex(close.index).fillna(close)
    volume = pd.to_numeric(history.get("Volume", pd.Series(index=close.index, dtype=float)), errors="coerce").reindex(close.index)
    latest = float(close.iloc[-1])
    mas = {period: float(close.tail(period).mean()) if len(close) >= period else np.nan for period in (5, 20, 50, 100, 200)}
    previous_volume = volume.iloc[-21:-1].replace(0, np.nan).dropna()
    volume_ratio = (float(volume.iloc[-1]) / float(previous_volume.mean()) * 100) if len(previous_volume) >= 5 and pd.notna(volume.iloc[-1]) else np.nan
    high20 = float(high.tail(20).max()) if len(high) >= 20 else np.nan
    gap20 = (latest / mas[20] - 1) * 100 if math.isfinite(mas[20]) and mas[20] else np.nan
    high20_distance = max(0.0, (high20 - latest) / high20 * 100) if math.isfinite(high20) and high20 else np.nan
    stack = all(math.isfinite(mas[x]) for x in (5,20,50,100,200)) and mas[5] > mas[20] > mas[50] > mas[100] > mas[200]
    last_volume = float(volume.iloc[-1]) if pd.notna(volume.iloc[-1]) else np.nan
    result = {
        "ticker": stock.ticker,
        "name": stock.name,
        "exchange": stock.exchange,
        "market": stock.market,
        "indexes": list(stock.indexes),
        "preferred": stock.preferred,
        "spac": stock.spac,
        "asset_type": stock.asset_type,
        "leveraged": stock.leveraged,
        "inverse": stock.inverse,
        "size_value": finite(stock.size_value, 0),
        "size_kind": "aum" if stock.asset_type == "etf" else "market_cap",
        "date": close.index[-1].strftime("%Y-%m-%d"),
        "close": finite(latest, 3),
        "day": trailing_return(close, 1),
        "w1": trailing_return(close, 5),
        "m1": trailing_return(close, 21),
        "m3": trailing_return(close, 63),
        "ma5": finite(mas[5], 3),
        "ma20": finite(mas[20], 3),
        "ma50": finite(mas[50], 3),
        "ma100": finite(mas[100], 3),
        "ma200": finite(mas[200], 3),
        "ma5_20": bool(math.isfinite(mas[5]) and math.isfinite(mas[20]) and mas[5] > mas[20]),
        "ma20_50": bool(math.isfinite(mas[20]) and math.isfinite(mas[50]) and mas[20] > mas[50]),
        "ma50_100": bool(math.isfinite(mas[50]) and math.isfinite(mas[100]) and mas[50] > mas[100]),
        "ma100_200": bool(math.isfinite(mas[100]) and math.isfinite(mas[200]) and mas[100] > mas[200]),
        "above200": bool(math.isfinite(mas[200]) and latest > mas[200]),
        "stack": bool(stack),
        "volume": finite(last_volume, 0),
        "volume_ratio": finite(volume_ratio, 2),
        "value_traded": finite(latest * last_volume, 0) if math.isfinite(last_volume) else None,
        "gap20": finite(gap20, 2),
        "high20_distance": finite(high20_distance, 2),
    }
    return result


def download_metrics(stocks: list[Stock], batch_size: int, pause: float) -> tuple[list[dict[str, object]], list[str]]:
    output: list[dict[str, object]] = []
    failed: list[str] = []
    all_batches = list(chunks(stocks, batch_size))
    for batch_no, batch in enumerate(all_batches, start=1):
        tickers = [stock.ticker for stock in batch]
        log(f"가격 수집 {batch_no}/{len(all_batches)}: {len(tickers)}종목")
        downloaded: pd.DataFrame | None = None
        for attempt in range(1, 4):
            try:
                downloaded = yf.download(
                    tickers=tickers,
                    period="18mo",
                    interval="1d",
                    group_by="ticker",
                    auto_adjust=True,
                    actions=False,
                    threads=True,
                    progress=False,
                    timeout=40,
                    multi_level_index=True,
                )
                if downloaded is not None and not downloaded.empty:
                    break
            except Exception as exc:
                log(f"배치 재시도 {attempt}/3: {exc}")
            time.sleep(4 * attempt)
        for stock in batch:
            try:
                history = extract_history(downloaded, stock.ticker, len(batch)) if downloaded is not None else None
                metric = compute(stock, history) if history is not None else None
                if metric is None:
                    failed.append(stock.ticker)
                else:
                    output.append(metric)
            except Exception as exc:
                log(f"계산 실패 {stock.ticker}: {exc}")
                failed.append(stock.ticker)
        time.sleep(pause)
    return output, failed


def existing_payload(path: Path) -> dict[str, object] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    except Exception:
        return None


def save(market: str, metrics: list[dict[str, object]], failed: list[str], total: int) -> None:
    path = DATA_DIR / f"{market}.json"
    old = existing_payload(path)
    old_count = len((old or {}).get("stocks", []))
    minimum = 100 if market == "kr" else 50
    if len(metrics) < minimum and old_count > len(metrics):
        log(f"성공 종목이 너무 적어 기존 데이터 {old_count}개를 보존합니다.")
        old["status"] = "warning"
        old["message"] = f"이번 자동 갱신에 실패해 이전 데이터를 유지했습니다. 성공 {len(metrics)}개 / 전체 {total}개"
        old["last_attempt_at"] = datetime.now(timezone.utc).isoformat()
        path.write_text(json.dumps(old, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        return
    dates = [str(x.get("date")) for x in metrics if x.get("date")]
    price_date = max(dates) if dates else None
    payload = {
        "market": market.upper(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "price_date": price_date,
        "status": "ok" if len(failed) == 0 else "partial",
        "message": f"전체 {total:,}개 중 {len(metrics):,}개 종목을 갱신했습니다. 실패 {len(failed):,}개.",
        "failed_count": len(failed),
        "failed_examples": failed[:30],
        "stocks": sorted(metrics, key=lambda x: (str(x.get("exchange")), str(x.get("name")))),
    }
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    tmp.replace(path)
    log(payload["message"])


def sample_listing(market: str) -> list[Stock]:
    if market == "kr":
        return [Stock("005930.KS", "삼성전자", "KOSPI", "KR"), Stock("000660.KS", "SK하이닉스", "KOSPI", "KR"), Stock("035420.KS", "NAVER", "KOSPI", "KR")]
    return [Stock("AAPL", "Apple", "US", "US", ("S&P500","NASDAQ100")), Stock("NVDA", "NVIDIA", "US", "US", ("S&P500","NASDAQ100")), Stock("MSFT", "Microsoft", "US", "US", ("S&P500","NASDAQ100"))]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--market", choices=("kr", "us"), required=True)
    parser.add_argument("--sample", action="store_true", help="개발 테스트용 소수 종목")
    parser.add_argument("--batch-size", type=int, default=50)
    parser.add_argument("--pause", type=float, default=1.2)
    args = parser.parse_args()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        stocks = sample_listing(args.market) if args.sample else (get_kr_listing() if args.market == "kr" else get_us_listing())
        log(f"대상 종목: {len(stocks)}개")
        metrics, failed = download_metrics(stocks, max(1, args.batch_size), max(0, args.pause))
        save(args.market, metrics, failed, len(stocks))
        return 0 if metrics else 2
    except Exception as exc:
        log(f"치명적 오류: {exc}")
        path = DATA_DIR / f"{args.market}.json"
        old = existing_payload(path) or {"market": args.market.upper(), "stocks": []}
        old.update({
            "status": "error",
            "message": f"자동 갱신 실패: {exc}",
            "last_attempt_at": datetime.now(timezone.utc).isoformat(),
        })
        path.write_text(json.dumps(old, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        return 1


if __name__ == "__main__":
    sys.exit(main())
