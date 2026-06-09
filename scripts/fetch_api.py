"""
fetch_api.py
============
공공데이터포털 복지 API 수집 모듈.
- 중앙부처복지서비스 (NationalWelfareInformationsV001)
- 지자체복지서비스 (LocalGovernmentWelfareInformations)
두 API 모두 XML 형식이며 페이지네이션을 지원.
"""

import requests
import xml.etree.ElementTree as ET
import time
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# API 엔드포인트
# ─────────────────────────────────────────────
NATIONAL_LIST_EP = (
    "https://apis.data.go.kr/B554287"
    "/NationalWelfareInformationsV001"
    "/NationalWelfarelistV001"          # 포털 Swagger 확인값
)
LOCAL_LIST_EP = (
    "https://apis.data.go.kr/B554287"
    "/LocalGovernmentWelfareInformations"
    "/LcgvWelfarelist"                  # 포털 상세기능정보 확인값
)

MAX_ROWS = 100       # 페이지당 최대 수신 건수 (API 한도)
RETRY_COUNT = 3      # 실패 시 재시도 횟수
PAGE_DELAY = 1.0     # 페이지 간 딜레이(초) - API 서버 보호


# ─────────────────────────────────────────────
# 공통 유틸
# ─────────────────────────────────────────────
def _get_text(element: ET.Element, tag: str) -> str:
    """XML 요소에서 텍스트 안전하게 추출."""
    el = element.find(tag)
    if el is None or el.text is None:
        return ""
    return el.text.strip()


def _fetch_page(url: str, params: dict) -> Optional[ET.Element]:
    """
    단일 페이지 GET 요청 + XML 파싱.
    실패 시 최대 RETRY_COUNT회 재시도.
    """
    for attempt in range(1, RETRY_COUNT + 1):
        try:
            resp = requests.get(url, params=params, timeout=30)
            resp.raise_for_status()

            # ── XML 파싱 ──
            root = ET.fromstring(resp.content)

            # API 결과 코드 확인 (00 또는 0000이 정상)
            result_code = (
                root.findtext(".//resultCode") or
                root.findtext(".//cmmMsgHeader/returnReasonCode") or
                "00"
            ).strip()
            if result_code not in ("00", "0000", "0", "000", ""):
                result_msg = root.findtext(".//resultMsg", "알 수 없는 오류")
                logger.error(f"API 오류 응답: [{result_code}] {result_msg}")
                return None

            return root

        except requests.exceptions.Timeout:
            logger.warning(f"타임아웃 (시도 {attempt}/{RETRY_COUNT}): {url}")
        except requests.exceptions.HTTPError as e:
            logger.warning(f"HTTP 오류 {e.response.status_code} (시도 {attempt}/{RETRY_COUNT})")
        except ET.ParseError as e:
            logger.error(f"XML 파싱 실패: {e}")
            return None  # XML 오류는 재시도 의미 없음
        except Exception as e:
            logger.warning(f"예외 발생 (시도 {attempt}/{RETRY_COUNT}): {e}")

        if attempt < RETRY_COUNT:
            wait = 2 ** attempt  # 지수 백오프: 2초, 4초
            logger.info(f"  {wait}초 후 재시도...")
            time.sleep(wait)

    logger.error(f"최대 재시도 횟수 초과: {url}")
    return None


# ─────────────────────────────────────────────
# 중앙부처 API 파서
# ─────────────────────────────────────────────
def _parse_national_item(item: ET.Element) -> Optional[dict]:
    """
    중앙부처 XML <servList> 하나를 dict로 변환.
    필드명은 포털 로그에서 직접 확인한 실제값 기준.
    """
    serv_id = _get_text(item, "servId")
    if not serv_id:
        return None

    # 주관부처 + 주관기관 합치기 (예: "국토교통부 주거복지지원과")
    ministry = _get_text(item, "jurMnofNm")
    dept     = _get_text(item, "jurOrgNm")
    org      = f"{ministry} {dept}".strip() if dept else ministry

    return {
        "id":           f"national_{serv_id}",
        "source":       "national",
        "serv_id":      serv_id,
        "title":        _get_text(item, "servNm"),
        "organization": org,
        # servDgst = 서비스 요약 → Gemini 가공의 핵심 입력값
        "content":      _get_text(item, "servDgst"),
        "target":       _get_text(item, "trgterIndvdlArray"),   # 대상자 배열
        "criteria":     "",                                      # 목록 API 미제공
        "apply_method": _get_text(item, "onapPsbltYn"),         # 온라인신청 가능 여부
        "contact":      _get_text(item, "rprsCntadr"),          # 대표 연락처
        "apply_url":    _get_text(item, "servDtlLink"),         # 복지로 상세 링크
        "life_cycle":   _get_text(item, "lifeArray"),           # 생애주기
        "support_type": _get_text(item, "intrsThemaArray"),     # 관심 주제
        "family_type":  "",
        "support_cycle": _get_text(item, "sprtCycNm"),
        # 중앙부처 = 전국 공통
        "region_raw":   "전국",
        "sido_raw":     "전국",
        "sgg_raw":      "",
    }


# ─────────────────────────────────────────────
# 지자체 API 파서
# ─────────────────────────────────────────────
def _parse_local_item(item: ET.Element) -> Optional[dict]:
    """
    지자체 XML <servList> 하나를 dict로 변환.
    필드명은 포털 로그에서 직접 확인한 실제값 기준.
    """
    serv_id = _get_text(item, "servId")
    if not serv_id:
        return None

    sido = _get_text(item, "ctpvNm")
    sgg  = _get_text(item, "sggNm")
    region_raw = f"{sido} {sgg}".strip() if sgg else sido

    return {
        "id":           f"local_{serv_id}",
        "source":       "local",
        "serv_id":      serv_id,
        "title":        _get_text(item, "servNm"),
        "organization": _get_text(item, "bizChrDeptNm"),        # 업무담당부서
        "content":      _get_text(item, "servDgst"),            # 서비스 요약
        "target":       _get_text(item, "trgterIndvdlNmArray"), # 대상자명 배열
        "criteria":     "",                                      # 목록 API 미제공
        "apply_method": _get_text(item, "aplyMtdNm"),          # 신청방법명
        "contact":      _get_text(item, "inqNum"),              # 문의번호
        "apply_url":    _get_text(item, "servDtlLink"),         # 복지로 상세 링크
        "life_cycle":   _get_text(item, "lifeNmArray"),         # 생애주기명
        "support_type": _get_text(item, "intrsThemaNmArray"),   # 관심주제명
        "family_type":  "",
        "support_cycle": _get_text(item, "sprtCycNm"),
        "region_raw":   region_raw,
        "sido_raw":     sido,
        "sgg_raw":      sgg,
    }


# ─────────────────────────────────────────────
# 전체 페이지 수집 (공통)
# ─────────────────────────────────────────────

# 복지로 API가 사용할 수 있는 아이템 태그명 후보 (우선순위 순)
_ITEM_TAG_CANDIDATES = [
    "servList",    # ✅ 복지로 API 실제 확인값 (국가·지자체 공통)
    "item",        # 공공데이터 API 표준
    "serv",
    "service",
    "wantedListDTO",
    "wantedVO",
    "lcgvItem",
    "lcgvVO",
    "servInfo",
    "welfareInfo",
    "row",
]

def _find_items(root: ET.Element, label: str) -> list:
    """
    XML에서 아이템 요소 목록을 유연하게 탐색.
    API마다 태그명이 다를 수 있으므로 여러 후보를 순서대로 시도.
    모두 실패하면 XML 구조를 로그에 출력해 디버깅 단서를 제공.
    """
    for tag in _ITEM_TAG_CANDIDATES:
        found = root.findall(f".//{tag}")
        if found:
            if tag != "item":
                logger.info(f"[{label}] 아이템 태그: <{tag}> 사용")
            return found

    # ── 모든 후보 실패 → XML 구조 덤프 ──
    # body 또는 루트의 직접 자식 요소 이름 출력
    children = [child.tag for child in root.iter()]
    unique_tags = list(dict.fromkeys(children))[:30]  # 중복 제거, 최대 30개
    logger.warning(
        f"[{label}] 알려진 태그로 아이템을 못 찾았습니다.\n"
        f"  XML에 존재하는 태그 목록: {unique_tags}\n"
        f"  XML 앞부분 (500자): {ET.tostring(root, encoding='unicode')[:500]}"
    )
    return []


def _fetch_all(url: str, base_params: dict, parse_fn, label: str) -> list:
    """
    페이지네이션을 돌며 전체 데이터를 수집.
    빈 페이지 또는 마지막 페이지 도달 시 종료.
    """
    all_items = []
    page = 1
    total_pages = None

    while True:
        params = {**base_params, "pageNo": page, "numOfRows": MAX_ROWS}
        logger.info(f"[{label}] 페이지 {page} 수집 중...")

        root = _fetch_page(url, params)
        if root is None:
            logger.error(f"[{label}] 페이지 {page} 수집 실패 → 중단")
            break

        # ── 첫 페이지에서 전체 건수 파악 ──
        if page == 1:
            total_str = (
                root.findtext(".//totalCount") or
                root.findtext(".//numOfRows") or  # 폴백
                "0"
            ).strip()
            try:
                total_count = int(total_str)
            except ValueError:
                total_count = 0

            if total_count > 0:
                total_pages = (total_count + MAX_ROWS - 1) // MAX_ROWS
                logger.info(f"[{label}] 전체 {total_count}건 / {total_pages}페이지")
            else:
                # totalCount가 없거나 0인 경우: 아이템 유무로 판단
                total_pages = 9999

        # ── 아이템 파싱 (유연한 태그 탐색) ──
        xml_items = _find_items(root, label)
        if not xml_items:
            if page == 1 and total_pages and total_pages > 1:
                # 1페이지인데 아이템이 없고 totalCount는 있음 → 태그명 문제
                logger.error(
                    f"[{label}] totalCount={total_count}이지만 아이템 파싱 실패. "
                    "위의 XML 태그 목록을 확인해 _ITEM_TAG_CANDIDATES에 추가 필요."
                )
            else:
                logger.info(f"[{label}] 더 이상 데이터 없음 → 완료")
            break

        for xml_item in xml_items:
            parsed = parse_fn(xml_item)
            if parsed:
                all_items.append(parsed)

        logger.info(f"[{label}] 페이지 {page}: {len(xml_items)}건 파싱 완료")

        # ── 종료 조건 ──
        if total_pages and page >= total_pages:
            logger.info(f"[{label}] 마지막 페이지 도달 → 완료")
            break

        # 마지막 페이지보다 적게 왔으면 종료
        if len(xml_items) < MAX_ROWS:
            logger.info(f"[{label}] 마지막 페이지 감지 → 완료")
            break

        page += 1
        time.sleep(PAGE_DELAY)

    return all_items


# ─────────────────────────────────────────────
# 외부 진입점
# ─────────────────────────────────────────────
def fetch_national_welfare(api_key: str) -> list:
    """중앙부처 복지서비스 전체 수집."""
    logger.info("━━ 중앙부처 API 수집 시작 ━━")
    params = {
        "serviceKey": api_key,
        "callTp": "L",
        "srchKeyCode": "001",   # 001 = 전체 조회 (포털 샘플 URL 확인값)
    }
    items = _fetch_all(NATIONAL_LIST_EP, params, _parse_national_item, "중앙부처")
    logger.info(f"━━ 중앙부처 수집 완료: {len(items)}건 ━━")
    return items


def fetch_local_welfare(api_key: str) -> list:
    """지자체 복지서비스 전체 수집."""
    logger.info("━━ 지자체 API 수집 시작 ━━")
    params = {
        "serviceKey": api_key,
        "srchKeyCode": "001",   # callTp 없음 - 지자체 API는 해당 파라미터 미지원
    }
    items = _fetch_all(LOCAL_LIST_EP, params, _parse_local_item, "지자체")
    logger.info(f"━━ 지자체 수집 완료: {len(items)}건 ━━")
    return items
