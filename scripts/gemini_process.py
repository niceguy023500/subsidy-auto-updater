"""
gemini_process.py
=================
google-genai (신규 공식 패키지) + gemini-2.5-flash 모델 사용.
- 구 패키지(google-generativeai)는 deprecated → 완전 교체
- 무료 티어 Rate Limit: 10 RPM → 6초 간격 적용
"""

import json
import time
import logging
import re
from typing import Optional

from google import genai
from google.genai import types

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# 모델 및 Rate Limit 설정
# ─────────────────────────────────────────────
MODEL_NAME       = "gemini-2.5-flash"   # 2026년 현재 무료 티어 권장 모델
RPM_LIMIT        = 10                   # 무료 티어 분당 최대 요청 수
REQUEST_INTERVAL = 60.0 / RPM_LIMIT    # 6.0초

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

_VALID = {
    "categories":      set(CATEGORIES),
    "age_groups":      set(AGE_GROUPS),
    "household_types": set(HOUSEHOLD_TYPES),
    "income_levels":   set(INCOME_LEVELS),
}


def setup_gemini(api_key: str):
    """google-genai 클라이언트 초기화."""
    client = genai.Client(api_key=api_key)
    logger.info(f"Gemini 클라이언트 초기화 완료 (모델: {MODEL_NAME})")
    return client


def _build_prompt(item: dict) -> str:
    title   = item.get("title",        "")
    content = item.get("content",      "")[:600]
    target  = item.get("target",       "")[:400]
    org     = item.get("organization", "")

    return f"""당신은 대한민국 복지 정책 전문가입니다.
아래 복지서비스 원문을 60세 이상 어르신도 이해할 수 있는 쉬운 말로 가공하세요.

[원문 정보]
서비스명: {title}
주관기관: {org}
서비스내용: {content}
지원대상: {target}

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
- categories는 반드시 1개 이상 선택"""


def _extract_json(raw_text: str) -> str:
    """Gemini 응답에서 JSON 블록 추출."""
    text = re.sub(r"```(?:json)?\s*", "", raw_text)
    text = text.replace("```", "").strip()
    start = text.find("{")
    end   = text.rfind("}") + 1
    if start == -1 or end == 0:
        raise ValueError("JSON 블록을 찾을 수 없음")
    return text[start:end]


def _validate_and_fix(result: dict) -> dict:
    """결과 검증 및 허용 값 외 항목 제거."""
    summary = result.get("summary", [])
    if not isinstance(summary, list):
        summary = []
    result["summary"] = (summary + ["", "", ""])[:3]

    if not isinstance(result.get("plain_desc"), str):
        result["plain_desc"] = ""

    for field, valid_set in _VALID.items():
        raw_list = result.get(field, [])
        if not isinstance(raw_list, list):
            raw_list = []
        filtered = [v for v in raw_list if v in valid_set]
        result[field] = filtered if filtered else (
            ["전체"]      if field in ("age_groups", "household_types") else
            ["소득무관"]  if field == "income_levels" else
            ["생활비·소득"]
        )
    return result


def process_item(client, item: dict, retries: int = 3) -> Optional[dict]:
    """단일 복지 아이템 Gemini 처리. 실패 시 최대 retries회 재시도."""
    prompt = _build_prompt(item)

    for attempt in range(1, retries + 1):
        try:
            response = client.models.generate_content(
                model=MODEL_NAME,
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=0.2,
                    max_output_tokens=800,
                ),
            )
            raw_text = response.text.strip()
            json_str = _extract_json(raw_text)
            result   = json.loads(json_str)
            result   = _validate_and_fix(result)
            return result

        except json.JSONDecodeError as e:
            logger.warning(f"JSON 파싱 실패 (시도 {attempt}/{retries}) [{item.get('id','?')}]: {e}")
        except ValueError as e:
            logger.warning(f"JSON 추출 실패 (시도 {attempt}/{retries}) [{item.get('id','?')}]: {e}")
        except Exception as e:
            logger.warning(f"Gemini 오류 (시도 {attempt}/{retries}) [{item.get('id','?')}]: {e}")

        if attempt < retries:
            time.sleep(5 * attempt)

    logger.error(f"Gemini 처리 최종 실패: {item.get('id','?')} / {item.get('title','')[:30]}")
    return None
