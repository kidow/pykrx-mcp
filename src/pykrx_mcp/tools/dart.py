"""DART Open API financial statement MCP tools.

Wraps the DART (Data Analysis, Retrieval and Transfer) Open API to expose
corporate financial statements (income statement, balance sheet, cash flow)
and corporate code lookup. Requires the DART_API_KEY environment variable.
"""

import io
import logging
import os
import xml.etree.ElementTree as ET
import zipfile

import httpx

from ..utils import format_error_response

logger = logging.getLogger(__name__)

DART_BASE_URL = "https://opendart.fss.or.kr/api"

# Report code -> human readable period (for documentation/validation)
VALID_PERIODS = {
    "11011": "사업보고서 (연간)",
    "11012": "반기보고서",
    "11013": "1분기보고서",
    "11014": "3분기보고서",
}

# Financial statement division codes used by fnlttSinglAcnt.json
_SJ_DIV = {
    "IS": "손익계산서",
    "BS": "재무상태표",
    "CF": "현금흐름표",
}


def _get_api_key() -> str | None:
    """Read the DART API key from the environment."""
    return os.getenv("DART_API_KEY")


def _request(endpoint: str, params: dict) -> dict:
    """
    Perform a synchronous GET request against the DART Open API.

    Args:
        endpoint: API endpoint path (e.g., "company.json")
        params: Query parameters (crtfc_key is injected automatically)

    Returns:
        Parsed JSON dict, or an error response dict.
    """
    api_key = _get_api_key()
    if not api_key:
        return format_error_response(
            "DART_API_KEY environment variable is not set. "
            "Get a key at https://opendart.fss.or.kr and set DART_API_KEY."
        )

    url = f"{DART_BASE_URL}/{endpoint}"
    query = {"crtfc_key": api_key, **params}

    try:
        response = httpx.get(url, params=query, timeout=30.0)
        response.raise_for_status()
        data = response.json()
    except httpx.HTTPStatusError as e:
        return format_error_response(
            f"DART API HTTP error: {e.response.status_code}", endpoint=endpoint
        )
    except httpx.HTTPError as e:
        return format_error_response(f"DART API request failed: {e}", endpoint=endpoint)
    except ValueError as e:
        return format_error_response(
            f"DART API returned invalid JSON: {e}", endpoint=endpoint
        )

    # DART returns status "000" on success; anything else is an error.
    status = data.get("status")
    if status and status != "000":
        return format_error_response(
            f"DART API error [{status}]: {data.get('message', 'unknown error')}",
            endpoint=endpoint,
        )

    return data


def _validate_period(period: str) -> tuple[bool, str]:
    """Validate the DART report code."""
    if period not in VALID_PERIODS:
        return False, (
            f"Invalid period code '{period}'. "
            f"Must be one of: {', '.join(VALID_PERIODS)} "
            "(11011=annual, 11012=half, 11013=Q1, 11014=Q3)"
        )
    return True, ""


def get_dart_corp_code(name: str) -> dict:
    """
    Look up a company's DART unique corporate code (corp_code) by name.

    Downloads the full DART corporate code list (corpCode.xml) and searches
    by company name. Returns all matching companies (partial match supported).

    Args:
        name: Company name to search (e.g., "삼성전자")

    Returns:
        Dictionary with matching companies (corp_code, corp_name, stock_code),
        or an error.
    """
    if not name or not name.strip():
        return format_error_response("Company name must not be empty", name=name)

    api_key = _get_api_key()
    if not api_key:
        return format_error_response(
            "DART_API_KEY environment variable is not set. "
            "Get a key at https://opendart.fss.or.kr and set DART_API_KEY."
        )

    try:
        response = httpx.get(
            f"{DART_BASE_URL}/corpCode.xml",
            params={"crtfc_key": api_key},
            timeout=30.0,
        )
        response.raise_for_status()
    except httpx.HTTPError as e:
        return format_error_response(
            f"DART corpCode.xml request failed: {e}", name=name
        )

    try:
        with zipfile.ZipFile(io.BytesIO(response.content)) as zf:
            xml_filename = next(
                (n for n in zf.namelist() if n.endswith(".xml")), zf.namelist()[0]
            )
            with zf.open(xml_filename) as f:
                tree = ET.parse(f)
    except (zipfile.BadZipFile, ET.ParseError, StopIteration) as e:
        return format_error_response(
            f"Failed to parse DART corp code list: {e}", name=name
        )

    root = tree.getroot()
    query = name.strip().lower()
    results = []
    for item in root.findall("list"):
        corp_name = item.findtext("corp_name", "")
        if query in corp_name.lower():
            stock_code = item.findtext("stock_code", "").strip()
            results.append(
                {
                    "corp_code": item.findtext("corp_code", "").strip(),
                    "corp_name": corp_name.strip(),
                    "stock_code": stock_code if stock_code else None,
                    "modify_date": item.findtext("modify_date", "").strip(),
                }
            )

    if not results:
        return format_error_response(
            f"No company found matching '{name}'", name=name
        )

    # Sort: exact match first, then listed companies (have stock code)
    results.sort(
        key=lambda r: (r["corp_name"].lower() != query, r["stock_code"] is None)
    )

    return {
        "query": name,
        "row_count": len(results),
        "data": results[:20],  # cap at 20 to avoid huge payloads
    }


def _get_financial_statement(
    corp_code: str, year: str, period: str, sj_div: str
) -> dict:
    """
    Shared helper to fetch and filter a single financial statement type.

    Args:
        corp_code: DART corporate code (8 digits)
        year: Business year in YYYY format (e.g., "2023")
        period: Report code (11011/11012/11013/11014)
        sj_div: Statement division - IS, BS, or CF

    Returns:
        Dictionary with filtered statement rows, or an error.
    """
    if not corp_code or not corp_code.strip():
        return format_error_response(
            "corp_code must not be empty", corp_code=corp_code
        )

    valid, msg = _validate_period(period)
    if not valid:
        return format_error_response(msg, period=period)

    data = _request(
        "fnlttSinglAcnt.json",
        {
            "corp_code": corp_code,
            "bsns_year": year,
            "reprt_code": period,
            "fs_div": "OFS",
        },
    )
    if "error" in data:
        return {**data, "corp_code": corp_code, "year": year, "period": period}

    rows = data.get("list", [])
    filtered = [row for row in rows if row.get("sj_div") == sj_div]

    if not filtered:
        return format_error_response(
            f"No {_SJ_DIV.get(sj_div, sj_div)} data found",
            corp_code=corp_code,
            year=year,
            period=period,
        )

    return {
        "corp_code": corp_code,
        "year": year,
        "period": period,
        "statement": _SJ_DIV.get(sj_div, sj_div),
        "row_count": len(filtered),
        "data": filtered,
    }


def get_dart_income_statement(
    corp_code: str, year: str, period: str = "11011"
) -> dict:
    """
    Retrieve a company's income statement (손익계산서) from DART.

    Args:
        corp_code: DART corporate code (8 digits, from get_dart_corp_code)
        year: Business year in YYYY format (e.g., "2023")
        period: Report code - 11011(annual), 11012(half), 11013(Q1), 11014(Q3)

    Returns:
        Dictionary with income statement line items, or an error.
    """
    return _get_financial_statement(corp_code, year, period, "IS")


def get_dart_balance_sheet(corp_code: str, year: str, period: str = "11011") -> dict:
    """
    Retrieve a company's balance sheet (재무상태표) from DART.

    Args:
        corp_code: DART corporate code (8 digits, from get_dart_corp_code)
        year: Business year in YYYY format (e.g., "2023")
        period: Report code - 11011(annual), 11012(half), 11013(Q1), 11014(Q3)

    Returns:
        Dictionary with balance sheet line items, or an error.
    """
    return _get_financial_statement(corp_code, year, period, "BS")


def get_dart_cash_flow(corp_code: str, year: str, period: str = "11011") -> dict:
    """
    Retrieve a company's cash flow statement (현금흐름표) from DART.

    Args:
        corp_code: DART corporate code (8 digits, from get_dart_corp_code)
        year: Business year in YYYY format (e.g., "2023")
        period: Report code - 11011(annual), 11012(half), 11013(Q1), 11014(Q3)

    Returns:
        Dictionary with cash flow line items, or an error.
    """
    return _get_financial_statement(corp_code, year, period, "CF")
