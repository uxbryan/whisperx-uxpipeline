"""
core/role_classify.py — Step 6: 角色辨識

輸入：speaker 樣本 dict + 會議類型
輸出：SPEAKER_XX → 角色名稱的 mapping

會議類型：
  - "interview"  訪談者 / 受訪者
  - "lecture"    老師 / 學生
  - "other"      不分角色，標 Speaker A/B/C...（跳過 LLM）
"""

from __future__ import annotations

import json
import re
from typing import Literal

from .llm import call

MeetingType = Literal["interview", "lecture", "other"]

DEFAULT_MODEL = "anthropic/claude-haiku-4-5"


def _generic_labels(speaker_ids: list[str]) -> dict[str, str]:
    """meeting_type=other：照出現順序標 Speaker A、B、C…"""
    labels = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    return {sid: f"Speaker {labels[i % 26]}" for i, sid in enumerate(speaker_ids)}


def _build_prompt(samples: dict[str, list[str]], meeting_type: MeetingType) -> str:
    role_options = {
        "interview": ["訪談者", "受訪者"],
        "lecture": ["老師", "學生"],
    }[meeting_type]

    lines = [
        f"以下是一場「{meeting_type}」錄音中，每位說話者的前幾句話。",
        f"請將每位 SPEAKER 分類為以下角色之一：{' / '.join(role_options)}",
        "",
    ]
    for sid, samples_list in samples.items():
        lines.append(f"【{sid}】")
        for s in samples_list:
            lines.append(f"  - {s}")
        lines.append("")

    lines.extend([
        "請回覆 JSON 格式，key 為 SPEAKER 編號，value 為角色：",
        '範例：{"SPEAKER_00": "訪談者", "SPEAKER_01": "受訪者"}',
        "",
        "只輸出 JSON，不要其他文字。",
    ])
    return "\n".join(lines)


def _parse_response(response: str, speaker_ids: list[str], fallback: dict[str, str]) -> dict[str, str]:
    """從 LLM 回應解析 JSON，失敗則 fallback 到 generic labels。"""
    m = re.search(r"\{[^{}]*\}", response, re.DOTALL)
    if not m:
        return fallback
    try:
        parsed = json.loads(m.group(0))
        # 確保每個 speaker 都有 mapping，缺的用 fallback 補
        result = dict(fallback)
        for sid in speaker_ids:
            if sid in parsed and isinstance(parsed[sid], str):
                result[sid] = parsed[sid]
        return result
    except json.JSONDecodeError:
        return fallback


def classify(
    samples: dict[str, list[str]],
    meeting_type: MeetingType,
    *,
    model: str = DEFAULT_MODEL,
) -> tuple[dict[str, str], float]:
    """回傳 (role_map, 估算成本 NTD)。

    meeting_type="other" 跳過 LLM，成本為 0。
    """
    speaker_ids = list(samples.keys())
    generic = _generic_labels(speaker_ids)

    if meeting_type == "other" or not speaker_ids:
        return generic, 0.0

    prompt = _build_prompt(samples, meeting_type)
    response, cost = call(model, prompt, source="voice-to-text.role_classify")
    role_map = _parse_response(response, speaker_ids, fallback=generic)
    return role_map, cost
