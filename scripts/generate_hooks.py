#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
generate_hooks.py
==================
welfare.json의 각 항목에 대해 Claude API(Batch)를 사용하여
'후킹 제목(hook_title)'과 '후킹 설명(hook_desc)'을 생성한다.

핵심 원칙
---------
1. 절대 사실관계(금액/자격/기관명/숫자)를 왜곡하지 않는다. 톤만 바꾼다.
2. 신규/변경된 항목만 처리한다 (cache/hooks_cache.json으로 추적).
   -> 매일 자동 실행되어도 비용이 거의 들지 않는다.
3. Batch API(50% 할인) 사용. 동기 호출 대비 비용 절반.
4. 실패 시 원본 title/plain_desc를 그대로 fallback으로 사용 (서비스 중단 없음).

사용 위치
---------
scripts/generate_hooks.py 로 저장한 뒤,
scripts/main.py 의 build_json.py 호출 "이후" 단계에 추가 실행한다.
(welfare.json이 이미 만들어진 다음, 그 파일을 읽어서 보강하는 후처리 스크립트)

필요 환경변수
-------------
ANTHROPIC_API_KEY  (GitHub Secrets에 등록)

실행 예시
---------
python scripts/generate_hooks.py --input data/welfare.json --output data/welfare.json
"""

import os
import sys
import json
import time
import hashlib
import argparse
from pathlib import Path

try:
    import anthropic
except ImportError:
    print("ERROR: pip install anthropic 필요", file=sys.stderr)
    sys.exit(1)


MODEL = "claude-haiku-4-5-20251001"
CACHE_PATH = "data/cache/hooks_cache.json"
BATCH_CHUNK_SIZE = 5000  # Batch API 1회 요청 최대치 내에서 조절
POLL_INTERVAL_SEC = 20
MAX_POLL_MINUTES = 60

SYSTEM_PROMPT = """\
당신은 대한민국 정부 복지 지원금 공고문을, 일반 국민(특히 고령층 포함)이 \
한눈에 이해하고 클릭하고 싶게 만드는 짧은 안내문으로 바꾸는 전문 카피라이터입니다.

절대 규칙 (반드시 지킬 것):
1. 금액, 숫자, 자격 조건, 기관명, 신청 대상 등 사실관계는 절대 새로 만들거나 바꾸지 않습니다. \
원문에 있는 사실만 사용하고, 원문에 없는 정보는 추가하지 않습니다.
2. 과장된 표현("무조건", "100% 받는다", "누구나" 등 사실과 다를 수 있는 단정)은 쓰지 않습니다.
3. 행정 용어("~사업을 시행한다", "~을 도모함", "~지원에 관한 규정") 대신, \
실제 사람이 말하듯 쉬운 구어체로 풀어씁니다.
4. 존댓말을 유지하되 친근하고 따뜻한 톤을 사용합니다.
5. hook_title은 핵심 혜택이 한눈에 보이도록 15~22자 내외로 작성합니다. \
(예: "월 30만원 받는 청년 월세 지원" 같은 형태)
6. hook_desc는 "누가, 무엇을, 어떻게" 받을 수 있는지 한 문장으로 30~55자 내외로 작성합니다.
7. 절대 이모지, 느낌표 남발, 클릭베이트성 거짓 약속을 사용하지 않습니다.

반드시 아래 JSON 형식으로만 응답하십시오. 다른 설명, 마크다운, 코드블록 없이 순수 JSON 한 줄만:
{"hook_title": "...", "hook_desc": "..."}
"""


def build_user_prompt(item: dict) -> str:
    title = (item.get("title") or "").strip()
    desc = (item.get("plain_desc") or "").strip()
    org = (item.get("organization") or "").strip()
    cats = ", ".join(item.get("categories") or [])
    age = ", ".join(item.get("age_groups") or [])
    return (
        f"[원본 제목] {title}\n"
        f"[원본 설명] {desc}\n"
        f"[지원 기관] {org}\n"
        f"[분야] {cats}\n"
        f"[대상 연령] {age}\n"
    )


def item_hash(item: dict) -> str:
    """캐시 무효화 판단용 해시 (원본 내용이 바뀌면 재생성)"""
    raw = json.dumps(
        {
            "title": item.get("title"),
            "plain_desc": item.get("plain_desc"),
            "organization": item.get("organization"),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def load_cache(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_cache(path: str, cache: dict):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")


def parse_model_json(text: str) -> dict:
    """모델 출력에서 JSON만 안전하게 추출"""
    text = text.strip()
    # 코드블록 등 잡음 제거
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:].strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("JSON 형식을 찾을 수 없음")
    return json.loads(text[start:end + 1])


def run_batch(client: anthropic.Anthropic, targets: list[dict]) -> dict:
    """
    targets: [{custom_id, item}, ...]
    return: {custom_id: {"hook_title":..., "hook_desc":...}}
    """
    requests = []
    for t in targets:
        requests.append({
            "custom_id": t["custom_id"],
            "params": {
                "model": MODEL,
                "max_tokens": 300,
                "system": SYSTEM_PROMPT,
                "messages": [
                    {"role": "user", "content": build_user_prompt(t["item"])}
                ],
            },
        })

    print(f"  Batch 생성: {len(requests)}건 요청...")
    batch = client.messages.batches.create(requests=requests)
    batch_id = batch.id
    print(f"  Batch ID: {batch_id} (상태: {batch.processing_status})")

    elapsed = 0
    while True:
        time.sleep(POLL_INTERVAL_SEC)
        elapsed += POLL_INTERVAL_SEC
        batch = client.messages.batches.retrieve(batch_id)
        print(f"  [{elapsed}s] 상태: {batch.processing_status}")
        if batch.processing_status == "ended":
            break
        if elapsed > MAX_POLL_MINUTES * 60:
            print("  WARNING: 폴링 타임아웃, 현재까지 결과만 사용")
            break

    results = {}
    for entry in client.messages.batches.results(batch_id):
        cid = entry.custom_id
        if entry.result.type != "succeeded":
            print(f"  실패: {cid} ({entry.result.type})")
            continue
        try:
            content_blocks = entry.result.message.content
            text = "".join(
                b.text for b in content_blocks if getattr(b, "type", "") == "text"
            )
            parsed = parse_model_json(text)
            if "hook_title" in parsed and "hook_desc" in parsed:
                results[cid] = {
                    "hook_title": parsed["hook_title"].strip(),
                    "hook_desc": parsed["hook_desc"].strip(),
                }
        except Exception as e:
            print(f"  파싱 실패: {cid} ({e})")

    return results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", default="data/welfare.json")
    ap.add_argument("--output", default="data/welfare.json")
    ap.add_argument("--cache", default=CACHE_PATH)
    ap.add_argument("--limit", type=int, default=0, help="테스트용: 처리 건수 제한 (0=전체)")
    args = ap.parse_args()

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("ERROR: ANTHROPIC_API_KEY 환경변수가 없습니다.", file=sys.stderr)
        sys.exit(1)

    client = anthropic.Anthropic(api_key=api_key)

    data = json.loads(Path(args.input).read_text(encoding="utf-8"))
    items = data.get("items", [])
    cache = load_cache(args.cache)

    # 1) 캐시 대조 -> 신규/변경 항목만 추출
    targets = []
    for idx, item in enumerate(items):
        iid = str(item.get("id") or idx)
        h = item_hash(item)
        cached = cache.get(iid)
        if cached and cached.get("hash") == h:
            # 캐시 적중: 기존 hook 재사용
            item["hook_title"] = cached["hook_title"]
            item["hook_desc"] = cached["hook_desc"]
            continue
        targets.append({"custom_id": iid, "item": item, "hash": h})

    if args.limit:
        targets = targets[: args.limit]

    print(f"전체 {len(items)}건 중 신규/변경 {len(targets)}건 처리 대상")

    if not targets:
        print("처리할 항목 없음. 종료.")
        Path(args.output).write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return

    # 2) Batch API 청크 단위 처리
    all_results = {}
    for i in range(0, len(targets), BATCH_CHUNK_SIZE):
        chunk = targets[i: i + BATCH_CHUNK_SIZE]
        print(f"청크 {i // BATCH_CHUNK_SIZE + 1}: {len(chunk)}건")
        chunk_results = run_batch(client, chunk)
        all_results.update(chunk_results)

    # 3) 결과를 items에 반영 + 캐시 갱신 (실패분은 원본 fallback)
    success, fallback = 0, 0
    target_map = {t["custom_id"]: t for t in targets}
    for item in items:
        iid = str(item.get("id") or items.index(item))
        if iid not in target_map:
            continue
        t = target_map[iid]
        result = all_results.get(iid)
        if result:
            item["hook_title"] = result["hook_title"]
            item["hook_desc"] = result["hook_desc"]
            cache[iid] = {
                "hash": t["hash"],
                "hook_title": result["hook_title"],
                "hook_desc": result["hook_desc"],
            }
            success += 1
        else:
            # 실패 시 원본 그대로 사용 (위젯은 hook_title 없으면 title로 자동 fallback)
            fallback += 1

    print(f"완료: 성공 {success}건 / 실패(원본유지) {fallback}건")

    save_cache(args.cache, cache)
    Path(args.output).write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"저장 완료: {args.output}")


if __name__ == "__main__":
    main()
