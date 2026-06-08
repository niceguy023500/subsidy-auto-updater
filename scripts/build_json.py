"""
build_json.py
=============
처리된 복지 아이템들을 welfare.json으로 빌드.
- 기존 데이터와 신규 데이터 병합
- API에서 사라진 항목 자동 제거
- 처리 완료 ID 캐시 관리 (증분 처리용)
"""

import json
import os
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# 경로 설정
# ─────────────────────────────────────────────
_BASE = os.path.join(os.path.dirname(__file__), "..")
WELFARE_JSON  = os.path.abspath(os.path.join(_BASE, "data", "welfare.json"))
CACHE_FILE    = os.path.abspath(os.path.join(_BASE, "data", "cache", "processed_ids.json"))

KST = timezone(timedelta(hours=9))


# ─────────────────────────────────────────────
# 로드 함수
# ─────────────────────────────────────────────
def load_existing_data() -> dict:
    """welfare.json 로드. 없으면 빈 구조 반환."""
    if os.path.exists(WELFARE_JSON):
        with open(WELFARE_JSON, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"metadata": {}, "items": []}


def load_cache() -> set:
    """processed_ids.json 로드. 없으면 빈 set 반환."""
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return set(data.get("processed_ids", []))
    return set()


# ─────────────────────────────────────────────
# 저장 함수
# ─────────────────────────────────────────────
def save_cache(processed_ids: set):
    """처리 완료 ID 목록을 캐시 파일에 저장."""
    os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(
            {"processed_ids": sorted(list(processed_ids))},
            f,
            ensure_ascii=False,
            indent=2,
        )
    logger.info(f"캐시 저장 완료: {len(processed_ids)}건")


def save_welfare_json(items: list, total_fetched: int) -> dict:
    """
    최종 welfare.json 저장.
    프론트엔드가 fetch해서 사용하는 핵심 데이터 파일.
    """
    now = datetime.now(KST)
    data = {
        "metadata": {
            "last_updated":     now.isoformat(),
            "last_updated_kst": now.strftime("%Y년 %m월 %d일 %H:%M"),
            "total_count":      len(items),
            "total_fetched_from_api": total_fetched,
        },
        "items": items,
    }

    os.makedirs(os.path.dirname(WELFARE_JSON), exist_ok=True)
    with open(WELFARE_JSON, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    size_kb = os.path.getsize(WELFARE_JSON) / 1024
    logger.info(f"welfare.json 저장 완료: {len(items)}건 / {size_kb:.1f} KB")
    return data


# ─────────────────────────────────────────────
# 아이템 빌더
# ─────────────────────────────────────────────
def build_welfare_item(
    raw_item: dict,
    gemini_result: Optional[dict],
    region_info: dict,
) -> dict:
    """
    raw 아이템 + Gemini 결과 + 지역 정보를 합쳐
    welfare.json에 들어갈 최종 구조를 생성.

    gemini_result가 None이면 원문을 그대로 보존
    (Gemini 처리 실패 항목도 데이터에 포함해 누락 방지).
    """
    now_kst = datetime.now(KST).isoformat()
    gemini = gemini_result or {}

    # Gemini 실패 시 원문에서 요약을 직접 생성 (폴백)
    title = raw_item.get("title", "")
    if not gemini:
        fallback_summary = [
            title[:15] if title else "지원 정보",
            "자세한 내용은 해당 기관에 문의하세요",
            raw_item.get("organization", "")[:15] or "관계 기관",
        ]
        fallback_plain = (
            f"'{title}' 서비스입니다. "
            f"지원 대상 및 신청 방법은 {raw_item.get('organization', '해당 기관')}에 문의하세요."
        )
        logger.debug(f"Gemini 폴백 적용: {raw_item.get('id', '?')}")
    else:
        fallback_summary = gemini.get("summary", ["", "", ""])
        fallback_plain   = gemini.get("plain_desc", "")

    return {
        "id":           raw_item["id"],
        "source":       raw_item["source"],          # "national" | "local"
        "title":        title,
        "organization": raw_item.get("organization", ""),
        "summary":      fallback_summary,
        "plain_desc":   fallback_plain,
        "categories":   gemini.get("categories",     ["생활비·소득"]),
        "age_groups":   gemini.get("age_groups",     ["전체"]),
        "household_types": gemini.get("household_types", ["전체"]),
        "income_levels":   gemini.get("income_levels",   ["소득무관"]),
        "regions": {
            "sido":      region_info.get("sido",      ["전국"]),
            "districts": region_info.get("districts", ["전국"]),
        },
        "apply_url":    raw_item.get("apply_url", ""),
        "contact":      raw_item.get("contact", ""),
        "support_type": raw_item.get("support_type", ""),
        "support_cycle": raw_item.get("support_cycle", ""),
        "gemini_ok":    gemini_result is not None,   # 처리 성공 여부 플래그
        "updated_at":   now_kst,
    }
