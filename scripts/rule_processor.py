"""
rule_processor.py
=================
Gemini API 없이 공공데이터 API 원본 필드를 활용한
규칙 기반 분류 + 요약 생성 모듈.

무료 티어 한도(20건/일) 문제를 완전히 해소.
API 응답의 intrsThemaNmArray, lifeNmArray, trgterIndvdlArray 등을
그대로 매핑해 정확도도 충분히 확보.
"""

import logging
import re

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# 카테고리 매핑 키워드
# ─────────────────────────────────────────────
CATEGORY_KEYWORDS = {
    "주거·임대":      ["주거", "주택", "임대", "임차", "전세", "월세", "거주", "공공분양"],
    "의료·건강":      ["보건", "의료", "건강", "병원", "치료", "간병",
                       "진료", "재활", "의약품", "정신건강"],
    "임신·출산·육아": ["임신", "출산", "산모", "신생아", "육아", "보육",
                       "어린이집", "유치원", "산후", "아동", "어린이"],
    "교육·훈련":      ["교육", "훈련", "학습", "장학", "학비", "수업료",
                       "직업훈련", "기술습득", "학교", "문해", "청소년"],
    "취업·창업":      ["취업", "창업", "일자리", "구직", "고용", "직업",
                       "채용", "인턴", "근로", "직장"],
    "장애인지원":     ["장애", "장애인", "장애등급", "활동보조", "보조기기"],
    "노인돌봄":       ["노인", "어르신", "경로", "노령", "독거노인", "노인복지"],
    "문화·여가":      ["문화", "여가", "스포츠", "체육", "예술", "관광",
                       "여행", "공연", "도서"],
    "생활비·소득":    ["생계", "소득", "현금", "급여", "수당", "지원금",
                       "복지급여", "생활비", "긴급복지", "기초생활",
                       "차상위", "수급"],
}

# ─────────────────────────────────────────────
# 생애주기 매핑
# ─────────────────────────────────────────────
AGE_KEYWORDS = {
    "영유아(0-6세)":         ["영유아", "영아", "유아", "0세", "1세", "2세",
                               "3세", "4세", "5세", "6세"],
    "아동·청소년(7-18세)":   ["아동", "청소년", "학생", "초등", "중등",
                               "고등", "소년", "소녀"],
    "청년(19-34세)":          ["청년", "대학생", "20대", "30대 초"],
    "중장년(35-64세)":        ["중장년", "중년", "40대", "50대", "60대"],
    "노인(65세이상)":         ["노인", "어르신", "경로", "노령", "65세",
                               "70세", "80세"],
}

# ─────────────────────────────────────────────
# 가구유형 매핑
# ─────────────────────────────────────────────
HOUSEHOLD_KEYWORDS = {
    "1인가구":     ["1인", "단독", "독거", "혼자"],
    "한부모가구":  ["한부모", "모자", "부자", "편부", "편모"],
    "다자녀가구":  ["다자녀", "다둥이", "셋째", "3자녀", "다문화"],
    "노인가구":    ["노인가구", "노인세대", "노인부부"],
    "장애인가구":  ["장애인가구", "장애가구"],
    "다문화가구":  ["다문화", "결혼이민", "외국인"],
}

# ─────────────────────────────────────────────
# 소득기준 매핑
# ─────────────────────────────────────────────
INCOME_KEYWORDS = {
    "기초생활수급자":  ["기초생활", "수급자", "생계급여", "의료급여"],
    "차상위계층":      ["차상위"],
    "중위소득50%이하": ["중위소득 50", "중위소득50", "50% 이하", "50%이하"],
    "중위소득100%이하":["중위소득 100", "중위소득100", "100% 이하", "100%이하",
                        "중위소득 120", "중위소득120"],
}


# ─────────────────────────────────────────────
# 내부 유틸
# ─────────────────────────────────────────────
def _clean_text(text: str, max_len: int = 0) -> str:
    """제어문자 제거 + 공백 정리 + 길이 제한."""
    if not text:
        return ""
    # 제어문자(U+0000~U+001F, U+007F) 제거
    text = re.sub(r'[\x00-\x1F\x7F]', ' ', text)
    # 연속 공백 정리
    text = re.sub(r'\s+', ' ', text).strip()
    if max_len and len(text) > max_len:
        text = text[:max_len]
    return text


def _clean(text: str) -> str:
    """None 처리 + 소문자 변환."""
    return _clean_text(text or "")


def _match_any(text: str, keywords: list) -> bool:
    """키워드 목록 중 하나라도 text에 포함되면 True."""
    for kw in keywords:
        if kw in text:
            return True
    return False


def _extract_categories(item: dict) -> list:
    """
    카테고리 분류.
    오탐 방지를 위해 공식 분류 필드(주제·생애주기·제목)만 사용.
    설명 본문(servDgst)은 제외 — 관련 없는 단어 포함 가능성 있음.
    """
    official = " ".join([
        _clean(item.get("support_type", "")),  # intrsThemaNmArray (관심주제)
        _clean(item.get("life_cycle",   "")),  # lifeNmArray (생애주기)
        _clean(item.get("title",        "")),  # 서비스명
        _clean(item.get("target",       "")),  # 지원대상 (간결한 공식 필드)
    ])

    matched = []
    for cat, keywords in CATEGORY_KEYWORDS.items():
        if _match_any(official, keywords):
            matched.append(cat)

    return matched if matched else ["생활비·소득"]


def _extract_age_groups(item: dict) -> list:
    """연령대 분류."""
    life_raw = _clean(item.get("life_cycle", ""))    # lifeNmArray
    target   = _clean(item.get("target", ""))
    combined = " ".join([life_raw, target])

    matched = []
    for age, keywords in AGE_KEYWORDS.items():
        if _match_any(combined, keywords):
            matched.append(age)

    return matched if matched else ["전체"]


def _extract_household_types(item: dict) -> list:
    """가구유형 분류."""
    target   = _clean(item.get("target", ""))
    content  = _clean(item.get("content", ""))
    combined = " ".join([target, content])

    matched = []
    for hh, keywords in HOUSEHOLD_KEYWORDS.items():
        if _match_any(combined, keywords):
            matched.append(hh)

    return matched if matched else ["전체"]


def _extract_income_levels(item: dict) -> list:
    """소득기준 분류."""
    target   = _clean(item.get("target", ""))
    content  = _clean(item.get("content", ""))
    combined = " ".join([target, content])

    for income, keywords in INCOME_KEYWORDS.items():
        if _match_any(combined, keywords):
            return [income]

    return ["소득무관"]


def _build_summary(item: dict) -> list:
    """3줄 요약 생성 (규칙 기반)."""
    title   = _clean_text(item.get("title", ""),   20)
    content = _clean_text(item.get("content", ""))
    target  = _clean_text(item.get("target", ""))
    org     = _clean_text(item.get("organization", ""), 15)

    line1 = title if title else "복지 지원 서비스"

    if target:
        line2 = re.split(r'[,\.\n]', target)[0].strip()[:20]
    elif content:
        line2 = re.split(r'[,\.\n]', content)[0].strip()[:20]
    else:
        line2 = "지원 대상 확인 필요"

    line3 = org if org else "주관기관에 문의"

    return [line1, line2, line3]


def _build_plain_desc(item: dict) -> str:
    """쉬운 말 설명 생성 (servDgst 직접 활용)."""
    content = _clean(item.get("content", ""))
    title   = _clean(item.get("title", ""))
    org     = _clean(item.get("organization", ""))

    if content and len(content) > 10:
        # servDgst가 있으면 그대로 사용 (이미 공공기관이 작성한 요약)
        return content[:300]

    # 없으면 기본 문장 생성
    return f"'{title}' 서비스입니다. 자세한 내용은 {org}에 문의하세요."


# ─────────────────────────────────────────────
# 외부 진입점
# ─────────────────────────────────────────────
def process_item(item: dict) -> dict:
    """
    단일 복지 아이템을 규칙 기반으로 처리.
    Gemini API 불필요 — 속도/안정성 모두 우수.
    """
    return {
        "summary":         _build_summary(item),
        "plain_desc":      _build_plain_desc(item),
        "categories":      _extract_categories(item),
        "age_groups":      _extract_age_groups(item),
        "household_types": _extract_household_types(item),
        "income_levels":   _extract_income_levels(item),
    }


def process_items_bulk(items: list) -> list:
    """복수 아이템 일괄 처리."""
    results = []
    for i, item in enumerate(items, 1):
        result = process_item(item)
        results.append(result)
        if i % 500 == 0:
            logger.info(f"  규칙 처리 진행: {i}/{len(items)}건")
    return results
