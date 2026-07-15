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
from dataclasses import dataclass
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


def get_us_listing() -> list[Stock]:
    merged: dict[str, Stock] = {}
    for stock in get_sp500() + get_nasdaq100():
        old = merged.get(stock.ticker)
        if old:
            indexes = tuple(sorted(set(old.indexes + stock.indexes)))
            name = old.name if old.name != old.ticker else stock.name
            merged[stock.ticker] = Stock(stock.ticker, name, "US", "US", indexes)
        else:
            merged[stock.ticker] = stock
    if len(merged) < 450:
        raise RuntimeError(f"미국 종목 목록이 지나치게 적습니다: {len(merged)}개")
    return list(merged.values())


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
    base = close.iloc[-periods - 1]
    return finite((close.iloc[-1] / base - 1) * 100) if base else None


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
