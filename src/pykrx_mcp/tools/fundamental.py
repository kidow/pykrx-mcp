"""Fundamental data related MCP tools."""

import io
import logging
import os
import xml.etree.ElementTree as ET
import zipfile

import httpx

from pykrx import stock

from ..utils import (
    format_dataframe_response,
    format_error_response,
    mcp_tool_error_handler,
    validate_date_format,
    validate_ticker_format,
)

logger = logging.getLogger(__name__)

_DART_BASE_URL = "https://opendart.fss.or.kr/api"
_COMMON_PAR_VALUES = [5000, 2500, 1000, 500, 200, 100]


def _dart_company_info(corp_code: str, year: str) -> dict | None:
    """Fetch 보통주 발행주식총수 and par_val from DART stockTotqySttus.json."""
    api_key = os.getenv("DART_API_KEY")
    if not api_key:
        return None
    try:
        resp = httpx.get(
            f"{_DART_BASE_URL}/stockTotqySttus.json",
            params={
                "crtfc_key": api_key,
                "corp_code": corp_code,
                "bsns_year": year,
                "reprt_code": "11011",
            },
            timeout=30.0,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") != "000":
            return None
        rows = data.get("list", [])
        common = next((r for r in rows if r.get("se") == "보통주"), None)
        total = next((r for r in rows if r.get("se") == "합계"), None)
        if not common or not total:
            return None

        def parse_int(v):
            try:
                return int(str(v).replace(",", "").strip())
            except (ValueError, AttributeError):
                return None

        # 발행주식총수 (보통주, 유통+자기주식)
        common_shares = parse_int(common.get("istc_totqy"))
        # 합계 현재발행주식총수 = 전체 발행주식 (액면가 역산용)
        total_issued = parse_int(total.get("now_to_isu_stock_totqy"))
        return {"common_shares": common_shares, "total_issued": total_issued}
    except Exception:
        return None


def _corp_code_from_ticker(ticker: str) -> str | None:
    """Resolve DART corp_code from a 6-digit stock ticker via corpCode.xml."""
    api_key = os.getenv("DART_API_KEY")
    if not api_key:
        return None
    try:
        resp = httpx.get(
            f"{_DART_BASE_URL}/corpCode.xml",
            params={"crtfc_key": api_key},
            timeout=30.0,
        )
        resp.raise_for_status()
        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            xml_name = next(n for n in zf.namelist() if n.endswith(".xml"))
            with zf.open(xml_name) as f:
                root = ET.parse(f).getroot()
        for item in root.findall("list"):
            if item.findtext("stock_code", "").strip() == ticker:
                return item.findtext("corp_code", "").strip()
    except Exception:
        pass
    return None


def _dart_statement(corp_code: str, year: str, sj_div: str) -> list[dict]:
    """Fetch a single DART financial statement; returns list of account rows."""
    api_key = os.getenv("DART_API_KEY")
    if not api_key:
        return []
    try:
        resp = httpx.get(
            f"{_DART_BASE_URL}/fnlttSinglAcnt.json",
            params={
                "crtfc_key": api_key,
                "corp_code": corp_code,
                "bsns_year": year,
                "reprt_code": "11011",
                "fs_div": "CFS",
            },
            timeout=30.0,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") != "000":
            return []
        return [r for r in data.get("list", []) if r.get("sj_div") == sj_div]
    except Exception:
        return []


def _parse_amount(val: str) -> int | None:
    try:
        return int(val.replace(",", "").replace(" ", ""))
    except (ValueError, AttributeError):
        return None


def _dart_fallback(ticker: str, start_date: str, end_date: str) -> dict:
    """
    Compute fundamental metrics from DART + Naver OHLCV when KRX is unavailable.

    EPS/BPS/PER/PBR are estimated using 자본금 / par_value for shares outstanding.
    Par value is guessed by trying common Korean par values (5000, 2500, …, 100)
    and selecting the first that yields a positive EPS (net income / shares > 0).
    Mark result as estimated when par value is guessed.
    """
    corp_code = _corp_code_from_ticker(ticker)
    if not corp_code:
        return format_error_response(
            "KRX API unavailable and DART_API_KEY not set — cannot compute fallback.",
            ticker=ticker,
        )

    # Try 최근 사업연도 (try last completed year, then prior year)
    from datetime import datetime
    current_year = datetime.now().year
    fs_rows: list[dict] = []
    used_year = None
    for year_offset in range(0, 3):
        year = str(current_year - 1 - year_offset)
        rows = _dart_statement(corp_code, year, "IS")
        if rows:
            fs_rows = rows
            used_year = year
            break

    if not fs_rows:
        return format_error_response(
            "KRX API unavailable and DART income statement also unavailable.",
            ticker=ticker,
        )

    bs_rows = _dart_statement(corp_code, used_year, "BS")

    # Extract key financials (CFS preferred, already filtered above)
    def find(rows, name_substr):
        for r in rows:
            if name_substr in r.get("account_nm", ""):
                v = _parse_amount(r.get("thstrm_amount", ""))
                if v is not None:
                    return v
        return None

    net_income = find(fs_rows, "당기순이익")
    equity = find(bs_rows, "자본총계")
    debt = find(bs_rows, "부채총계")
    paid_in_capital = find(bs_rows, "자본금")
    revenue = find(fs_rows, "매출액")
    op_income = find(fs_rows, "영업이익")

    # Current price from Naver OHLCV
    df_ohlcv = stock.get_market_ohlcv(
        fromdate=start_date, todate=end_date, ticker=ticker
    )
    close_price = int(df_ohlcv["종가"].iloc[-1]) if not df_ohlcv.empty else None

    # Resolve shares outstanding via DART company.json (par_val + list_stock_cnt)
    eps = bps = per = pbr = shares = None
    par_value_used = None
    par_value_estimated = False

    company_info = _dart_company_info(corp_code, used_year)
    if company_info:
        shares = company_info.get("common_shares")
        # Derive par value: total_issued × par_val = paid_in_capital
        total_issued = company_info.get("total_issued")
        if paid_in_capital and total_issued and total_issued > 0:
            par_value_used = round(paid_in_capital / total_issued)
        par_value_estimated = False

    # Fallback to par value heuristic only when company.json unavailable
    if not shares and paid_in_capital and net_income is not None and equity:
        for pv in _COMMON_PAR_VALUES:
            candidate_shares = paid_in_capital // pv
            if candidate_shares <= 0:
                continue
            candidate_eps = net_income / candidate_shares
            if candidate_eps != 0:
                shares = candidate_shares
                eps = round(candidate_eps)
                bps = round(equity / shares)
                par_value_used = pv
                par_value_estimated = True
                break

    if shares and net_income is not None and equity:
        eps = round(net_income / shares)
        bps = round(equity / shares)

    if close_price and eps:
        per = round(close_price / eps, 2) if eps > 0 else None
    if close_price and bps:
        pbr = round(close_price / bps, 2) if bps > 0 else None

    roe = round(net_income / equity * 100, 2) if (equity and net_income is not None) else None
    op_margin = round(op_income / revenue * 100, 2) if (revenue and op_income) else None
    debt_ratio = round(debt / equity * 100, 2) if (equity and debt) else None

    return {
        "ticker": ticker,
        "start_date": start_date,
        "end_date": end_date,
        "data_source": "dart_ohlcv_fallback",
        "dart_year": used_year,
        "note": (
            f"KRX API unavailable. Metrics from DART {used_year} CFS + Naver OHLCV. "
            + (f"상장주식수 from DART company.json (액면가 {par_value_used}원). " if not par_value_estimated and par_value_used else "")
            + (f"상장주식수 estimated using par value {par_value_used}원 (guessed). EPS/BPS/PER/PBR may be inaccurate." if par_value_estimated else "")
        ),
        "row_count": 1,
        "data": [{
            "날짜": end_date,
            "종가": close_price,
            "EPS": eps,
            "BPS": bps,
            "PER": per,
            "PBR": pbr,
            "ROE": roe,
            "영업이익률": op_margin,
            "부채비율": debt_ratio,
            "추정_상장주식수": shares,
            "추정_액면가": par_value_used,
        }],
    }


@mcp_tool_error_handler
def get_market_fundamental_by_date(ticker: str, start_date: str, end_date: str) -> dict:
    """
    Retrieve fundamental data (PER, PBR, dividend yield, etc.) for a stock.

    This tool fetches fundamental indicators that help evaluate stock valuation.
    Use this when you need to analyze a stock's fundamental metrics over time.

    Args:
        ticker: Stock ticker symbol (e.g., "005930" for Samsung Electronics)
        start_date: Start date in YYYYMMDD format (e.g., "20240101")
        end_date: End date in YYYYMMDD format (e.g., "20240131")

    Returns:
        Dictionary containing fundamental data including:
        - BPS (Book-value Per Share): 주당순자산가치
        - PER (Price Earnings Ratio): 주가수익비율
        - PBR (Price Book-value Ratio): 주가순자산비율
        - EPS (Earnings Per Share): 주당순이익
        - DIV (Dividend): 배당금
        - DPS (Dividend Per Share): 주당배당금

    Example:
        get_market_fundamental_by_date("005930", "20240101", "20240131")
        Returns Samsung Electronics fundamental data for January 2024
    """
    # Validate ticker format
    valid, msg = validate_ticker_format(ticker)
    if not valid:
        return format_error_response(msg, ticker=ticker)

    # Validate date formats
    valid, msg = validate_date_format(start_date)
    if not valid:
        return format_error_response(msg, start_date=start_date)

    valid, msg = validate_date_format(end_date)
    if not valid:
        return format_error_response(msg, end_date=end_date)

    # Fetch fundamental data (KRX API)
    df = stock.get_market_fundamental_by_date(
        fromdate=start_date, todate=end_date, ticker=ticker
    )

    if not df.empty:
        return format_dataframe_response(
            df, ticker=ticker, start_date=start_date, end_date=end_date
        )

    # KRX API unavailable — fallback to DART + Naver OHLCV
    return _dart_fallback(ticker, start_date, end_date)
