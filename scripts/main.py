"""
main.py
=======
복지 데이터 자동 업데이트 파이프라인 진입점.

실행 순서:
  1. 공공데이터포털 API에서 전체 복지 데이터 수집 (XML)
  2. 기존 welfare.json 및 캐시 로드
  3. 신규/미처리 항목 식별
  4. Gemini로 가공 (요약, 분류)
  5. 지역 정보 정규화
  6. welfare.json 저장
  7. 캐시 업데이트
"""

import os
import sys
import time
import logging
from datetime import datetime, timezone, timedelta

# ─────────────────────────────────────────────
# 로깅 설정 (GitHub Actions 로그에서 읽기 편하도록)
# ─────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
    force=True,
)
logger = logging.getLogger("main")

# ─────────────────────────────────────────────
# 환경변수 로드 (.env 지원 - 로컬 테스트용)
# ─────────────────────────────────────────────
try:
    from dotenv import load_dotenv
    load_dotenv()
    logger.info(".env 파일 로드 완료 (로컬 실행 모드)")
except ImportError:
    pass  # GitHub Actions에서는 .env 불필요

# ─────────────────────────────────────────────
# 필수 환경변수 확인
# ─────────────────────────────────────────────
WELFARE_API_KEY = os.environ.get("WELFARE_API_KEY", "").strip()
GEMINI_API_KEY  = os.environ.get("GEMINI_API_KEY",  "").strip()

if not WELFARE_API_KEY:
    logger.error("환경변수 WELFARE_API_KEY 가 없습니다. 종료.")
    sys.exit(1)

if not GEMINI_API_KEY:
    logger.error("환경변수 GEMINI_API_KEY 가 없습니다. 종료.")
    sys.exit(1)

# ─────────────────────────────────────────────
# 내부 모듈 import
# ─────────────────────────────────────────────
sys.path.insert(0, os.path.dirname(__file__))

from fetch_api      import fetch_national_welfare, fetch_local_welfare
from gemini_process import setup_gemini, process_item, REQUEST_INTERVAL
from region_mapper  import get_regions_for_item
from build_json     import (
    load_existing_data, load_cache, save_cache,
    build_welfare_item, save_welfare_json,
)

KST = timezone(timedelta(hours=9))

# ─────────────────────────────────────────────
# 유틸
# ─────────────────────────────────────────────
def _fmt_elapsed(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}시간 {m}분 {s}초"
    if m:
        return f"{m}분 {s}초"
    return f"{s}초"


# ─────────────────────────────────────────────
# 메인 파이프라인
# ─────────────────────────────────────────────
def main():
    total_start = time.time()
    now_kst = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S KST")

    logger.info("=" * 65)
    logger.info(f"  복지 데이터 자동 업데이트 시작: {now_kst}")
    logger.info("=" * 65)

    # ──────────────────────────────────────────
    # STEP 1: 공공데이터 API 수집
    # ──────────────────────────────────────────
    logger.info("")
    logger.info("▶ STEP 1: 공공데이터포털 API 수집")

    national_items = fetch_national_welfare(WELFARE_API_KEY)
    local_items    = fetch_local_welfare(WELFARE_API_KEY)
    all_raw_items  = national_items + local_items

    logger.info(
        f"  중앙부처 {len(national_items)}건 | "
        f"지자체 {len(local_items)}건 | "
        f"합계 {len(all_raw_items)}건"
    )

    if not all_raw_items:
        logger.error("수집된 데이터가 없습니다. 네트워크/API 키를 확인하세요. 종료.")
        sys.exit(1)

    # ──────────────────────────────────────────
    # STEP 2: 기존 데이터 & 캐시 로드
    # ──────────────────────────────────────────
    logger.info("")
    logger.info("▶ STEP 2: 기존 데이터 & 캐시 로드")

    existing_data  = load_existing_data()
    existing_map   = {item["id"]: item for item in existing_data.get("items", [])}
    processed_ids  = load_cache()

    logger.info(
        f"  기존 welfare.json: {len(existing_map)}건 | "
        f"처리 캐시: {len(processed_ids)}건"
    )

    # ──────────────────────────────────────────
    # STEP 3: 신규/미처리 항목 식별
    # ──────────────────────────────────────────
    logger.info("")
    logger.info("▶ STEP 3: 신규/미처리 항목 식별")

    current_api_ids = {item["id"] for item in all_raw_items}
    new_items = [item for item in all_raw_items if item["id"] not in processed_ids]

    logger.info(
        f"  신규/미처리: {len(new_items)}건 | "
        f"이미 처리됨: {len(all_raw_items) - len(new_items)}건"
    )

    # ──────────────────────────────────────────
    # STEP 4: Gemini 가공
    # ──────────────────────────────────────────
    logger.info("")
    logger.info("▶ STEP 4: Gemini 1.5 Flash 가공")

    model = setup_gemini(GEMINI_API_KEY)
    newly_processed = {}   # id → welfare_item
    last_req_time = 0.0
    gemini_ok_count = 0
    gemini_fail_count = 0

    for i, raw_item in enumerate(new_items, start=1):
        # Rate limit 적용
        elapsed = time.time() - last_req_time
        if elapsed < REQUEST_INTERVAL and last_req_time > 0:
            time.sleep(REQUEST_INTERVAL - elapsed)

        title_preview = raw_item.get("title", "")[:35]
        logger.info(f"  [{i:4d}/{len(new_items)}] {title_preview}")

        gemini_result = process_item(model, raw_item)
        last_req_time = time.time()

        region_info   = get_regions_for_item(raw_item)
        welfare_item  = build_welfare_item(raw_item, gemini_result, region_info)

        newly_processed[raw_item["id"]] = welfare_item
        processed_ids.add(raw_item["id"])

        if gemini_result:
            gemini_ok_count += 1
        else:
            gemini_fail_count += 1

    logger.info(
        f"  Gemini 성공: {gemini_ok_count}건 | "
        f"폴백 처리: {gemini_fail_count}건"
    )

    # ──────────────────────────────────────────
    # STEP 5: 최종 아이템 목록 구성
    #   - API에서 사라진 항목 제거
    #   - 기존 + 신규 병합
    # ──────────────────────────────────────────
    logger.info("")
    logger.info("▶ STEP 5: 최종 데이터 병합")

    final_items = []

    # 기존 항목 중 여전히 API에 있는 것 유지
    kept = 0
    dropped = 0
    for item_id, item in existing_map.items():
        if item_id in current_api_ids and item_id not in newly_processed:
            final_items.append(item)
            kept += 1
        elif item_id not in current_api_ids:
            dropped += 1  # API에서 사라진 항목 제거

    # 신규 처리된 항목 추가
    final_items.extend(newly_processed.values())

    # 캐시 정리: API에 없는 ID는 캐시에서도 제거
    processed_ids = processed_ids.intersection(current_api_ids)

    logger.info(
        f"  기존 유지: {kept}건 | "
        f"신규 추가: {len(newly_processed)}건 | "
        f"삭제(API 소멸): {dropped}건 | "
        f"최종: {len(final_items)}건"
    )

    # ──────────────────────────────────────────
    # STEP 6: 저장
    # ──────────────────────────────────────────
    logger.info("")
    logger.info("▶ STEP 6: 파일 저장")

    save_welfare_json(final_items, len(all_raw_items))
    save_cache(processed_ids)

    # ──────────────────────────────────────────
    # 완료 요약
    # ──────────────────────────────────────────
    elapsed_total = time.time() - total_start
    logger.info("")
    logger.info("=" * 65)
    logger.info(f"  ✅ 완료! 소요 시간: {_fmt_elapsed(elapsed_total)}")
    logger.info(f"  최종 복지 데이터: {len(final_items)}건")
    logger.info(f"  완료 시각: {datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S KST')}")
    logger.info("=" * 65)


if __name__ == "__main__":
    main()
