"""
gemini_process.py
=================
Gemini 1.5 Flash API로 복지서비스 원문을 가공.
- 3줄 요약 (고령층 눈높이)
- 쉬운 말 풀이
- 카테고리 / 연령대 / 가구유형 / 소득기준 자동 분류

무료 티어 제한: 분당 15회 → 인터벌 약 4.3초 적용.
"""

import json
import time
import logging
import re
from typing import Optional

import google.generativeai as genai

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# Gemini 무료 티어 Rate Limit
# ─────────────────────────────────────────────
RPM_LIMIT = 14          # 분당 14회 (15회 한도에 안전 마진 -1)
REQUEST_INTERVAL = 60.0 / RPM_LIMIT   # ≒ 4.3초

# ─────────────────────────────────────────────
# 분류 기준 (프론트엔드 필터와 완전 동일해야 함)
# ─────────────────────────────────────────────
CATEGORIES = [
    "생활비·소득", "주거·임대", "의료·건강", "임신·출산·육아",
    "교육·훈련", "취업·창업", "장애인지원", "노인돌봄", "문화·여가"
]
AGE_GROUPS = [
    "영유아(0-6세)", "아동·청소년(7-18세)", "청년(19-34세)",
    "중장년(35-64세)", "노인(65세이상)", "전체"
]
HOUSEHOLD_TYPES = [
    "1인가구", "한부모가구", "다자녀가구", "노인가구",
    "장애인가구", "다문화가구", "일반가구", "전체"
]
INCOME_LEVELS = [
    "기초생활수급자", "차상위계층", "중위소득50%이하",
    "중위소득100%이하", "소득무관"
]

# ─────────────────────────────────────────────
# 허용 값 집합 (검증용)
# ─────────────────────────────────────────────
_VALID = {
    "categories": set(CATEGORIES),
    "age_groups": set(AGE_GROUPS),
    "household_types": set(HOUSEHOLD_TYPES),
    "income_levels": set(INCOME_LEVELS),
}


def setup_gemini(api_key: str):
    """Gemini 모델 초기화."""
    genai.configure(api_key=api_key)
    model = genai.GenerativeModel(
        model_name="gemini-1.5-flash",
        generation_config={
            "temperature": 0.2,      # 낮을수록 일관된 JSON 출력
            "max_output_tokens": 800,
        },
    )
    logger.info("Gemini 1.5 Flash 모델 초기화 완료")
    return model


def _build_prompt(item: dict) -> str:
    """복지 아이템으로 Gemini 프롬프트 생성."""
    title    = item.get("title", "")
    target   = item.get("target", "")[:600]    # 너무 길면 토큰 낭비
    criteria = item.get("criteria", "")[:400]
    content  = item.get("content", "")[:600]
    org      = item.get("organization", "")

    return f"""당신은 대한민국 복지 정책 전문가입니다.
아래 복지서비스 원문을 60세 이상 어르신도 이해할 수 있는 쉬운 말로 가공하세요.

[원문 정보]
서비스명: {title}
주관기관: {org}
지원대상: {target}
선정기준: {criteria}
서비스내용: {content}

[출력 규칙]
반드시 아래 JSON 형식만 출력하세요. JSON 외 다른 텍스트는 절대 쓰지 마세요.

{{
  "summary": [
    "핵심 수혜 대상 한 줄 (15자 이내)",
    "주요 혜택 내용 한 줄 (15자 이내)",
    "신청 조건 또는 방법 한 줄 (15자 이내)"
  ],
  "plain_desc": "어르신도 이해할 수 있는 쉬운 말 2~3문장. 구체적 혜택(금액, 서비스 종류)을 반드시 포함.",
  "categories": [],
  "age_groups": [],
  "household_types": [],
  "income_levels": []
}}

[분류 선택 가능 값]
categories(해당 모두 선택): {json.dumps(CATEGORIES, ensure_ascii=False)}
age_groups(해당 모두 선택): {json.dumps(AGE_GROUPS, ensure_ascii=False)}
household_types(해당 모두 선택): {json.dumps(HOUSEHOLD_TYPES, ensure_ascii=False)}
income_levels(해당 모두 선택): {json.dumps(INCOME_LEVELS, ensure_ascii=False)}

[분류 판단 기준]
- 대상/조건이 불명확하면 "전체" 또는 "소득무관" 선택
- categories는 반드시 1개 이상 선택
- 복수 선택 가능"""


def _extract_json(raw_text: str) -> str:
    """
    Gemini 응답에서 JSON 블록 추출.
    ```json ... ``` 또는 순수 JSON 모두 처리.
    """
    # 마크다운 코드블록 제거
    text = re.sub(r"```(?:json)?\s*", "", raw_text)
    text = text.replace("```", "").strip()

    # 중괄호 범위만 추출
    start = text.find("{")
    end = text.rfind("}") + 1
    if start == -1 or end == 0:
        raise ValueError("JSON 블록을 찾을 수 없음")

    return text[start:end]


def _validate_and_fix(result: dict) -> dict:
    """
    Gemini 결과를 검증하고 허용 값 외의 항목을 제거.
    summary는 정확히 3개로 맞춤.
    """
    # summary 길이 보정
    summary = result.get("summary", [])
    if not isinstance(summary, list):
        summary = []
    result["summary"] = (summary + ["", "", ""])[:3]

    # plain_desc 기본값
    if not isinstance(result.get("plain_desc"), str):
        result["plain_desc"] = ""

    # 분류 필드: 허용 값만 통과
    for field, valid_set in _VALID.items():
        raw_list = result.get(field, [])
        if not isinstance(raw_list, list):
            raw_list = []
        filtered = [v for v in raw_list if v in valid_set]
        result[field] = filtered if filtered else (
            ["전체"] if field == "age_groups" else
            ["전체"] if field == "household_types" else
            ["소득무관"] if field == "income_levels" else
            ["생활비·소득"]   # categories 기본값
        )

    return result


def process_item(model, item: dict, retries: int = 3) -> Optional[dict]:
    """
    단일 복지 아이템을 Gemini로 처리.
    실패 시 최대 retries회 재시도.
    """
    prompt = _build_prompt(item)

    for attempt in range(1, retries + 1):
        try:
            response = model.generate_content(prompt)
            raw_text = response.text.strip()

            json_str = _extract_json(raw_text)
            result = json.loads(json_str)
            result = _validate_and_fix(result)

            return result

        except json.JSONDecodeError as e:
            logger.warning(
                f"JSON 파싱 실패 (시도 {attempt}/{retries}) "
                f"[{item.get('id', '?')}]: {e}"
            )
        except ValueError as e:
            logger.warning(
                f"JSON 추출 실패 (시도 {attempt}/{retries}) "
                f"[{item.get('id', '?')}]: {e}"
            )
        except Exception as e:
            logger.warning(
                f"Gemini 오류 (시도 {attempt}/{retries}) "
                f"[{item.get('id', '?')}]: {e}"
            )

        if attempt < retries:
            time.sleep(5 * attempt)  # 5초, 10초

    logger.error(f"Gemini 처리 최종 실패: {item.get('id', '?')} / {item.get('title', '')[:30]}")
    return None
