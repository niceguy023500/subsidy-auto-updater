"""
region_mapper.py
================
API 응답의 지역명 텍스트를 표준화된 시도/구군 구조로 매핑.
- 중앙부처 항목은 무조건 '전국'
- 지자체 항목은 sido_raw/sgg_raw를 분석하여 정규화
"""

import json
import os
import logging

logger = logging.getLogger(__name__)

REGION_FILE = os.path.join(
    os.path.dirname(__file__), "..", "data", "regions", "region_codes.json"
)

_region_data: dict = None
_sido_list: list = None
_aliases: dict = None


def _load():
    """지역 코드 JSON 최초 1회 로드 (메모리 캐시)."""
    global _region_data, _sido_list, _aliases
    if _region_data is not None:
        return

    with open(REGION_FILE, "r", encoding="utf-8") as f:
        _region_data = json.load(f)

    _sido_list = _region_data["sido"]
    _aliases = _region_data.get("aliases", {})


def _normalize_sido(raw: str) -> str:
    """
    raw 시도명을 공식 명칭으로 정규화.
    예) '경기' → '경기도', '전라북도' → '전북특별자치도'
    """
    _load()
    raw = raw.strip()

    # 1) 별칭 테이블 직접 매핑
    if raw in _aliases:
        return _aliases[raw]

    # 2) 공식 명칭 완전 일치
    for sido in _sido_list:
        if sido["name"] == raw:
            return sido["name"]

    # 3) short 이름 포함 여부
    for sido in _sido_list:
        if sido["short"] in raw:
            return sido["name"]

    # 4) raw가 공식명의 앞부분을 포함하는 경우 (예: '충청북' → '충청북도')
    for sido in _sido_list:
        if raw in sido["name"]:
            return sido["name"]

    return ""  # 매핑 실패


def _normalize_district(sido_name: str, raw_sgg: str) -> str:
    """
    raw 구군명을 해당 시도의 공식 구군명으로 정규화.
    완전 일치 → 부분 포함 순으로 시도.
    """
    _load()
    raw_sgg = raw_sgg.strip()
    if not raw_sgg:
        return ""

    sido_entry = next((s for s in _sido_list if s["name"] == sido_name), None)
    if not sido_entry:
        return ""

    districts = sido_entry["districts"]

    # 완전 일치
    if raw_sgg in districts:
        return raw_sgg

    # raw가 공식명을 포함 (예: '수원시 팔달구' → '수원시')
    for d in districts:
        if d in raw_sgg:
            return d

    # 공식명이 raw를 포함
    for d in districts:
        if raw_sgg in d:
            return d

    return ""


def get_regions_for_item(item: dict) -> dict:
    """
    아이템 dict를 받아 정규화된 지역 정보를 반환.

    Returns:
        {
            "sido": ["서울특별시"],         # 또는 ["전국"]
            "districts": ["종로구", "중구"] # 또는 ["전국"] 또는 ["전체"]
        }
    """
    _load()

    # ── 중앙부처는 무조건 전국 ──
    if item.get("source") == "national":
        return {"sido": ["전국"], "districts": ["전국"]}

    sido_raw = item.get("sido_raw", "").strip()
    sgg_raw = item.get("sgg_raw", "").strip()

    # ── 지역 정보 자체가 없으면 전국 처리 ──
    if not sido_raw or sido_raw in ("전국", "공통"):
        return {"sido": ["전국"], "districts": ["전국"]}

    # ── 시도 정규화 ──
    normalized_sido = _normalize_sido(sido_raw)
    if not normalized_sido:
        logger.warning(f"시도 매핑 실패: '{sido_raw}' → 전국으로 처리")
        return {"sido": ["전국"], "districts": ["전국"]}

    # ── 구군 정규화 ──
    if sgg_raw:
        normalized_sgg = _normalize_district(normalized_sido, sgg_raw)
        if normalized_sgg:
            districts = [normalized_sgg]
        else:
            logger.debug(f"구군 매핑 실패: '{sgg_raw}' in '{normalized_sido}' → 전체")
            districts = ["전체"]
    else:
        districts = ["전체"]  # 시도만 있고 구군 없음

    return {"sido": [normalized_sido], "districts": districts}
