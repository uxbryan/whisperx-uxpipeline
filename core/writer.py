"""
core/writer.py — 把 pipeline 結果寫成檔案到 NAS result/。

每個 job 一個資料夾，包含：
  - transcript.polished.txt   LLM 精修版（給人讀）
  - transcript.raw.txt        無 LLM 版本（搜尋友善、後備）
  - segments.json             結構化資料
  - speakers.json             speaker 樣本（角色判斷依據）
  - meta.json                 job metadata
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from .pipeline import Segment


def _fmt_ts(sec: float) -> str:
    m, s = divmod(int(sec), 60)
    return f"{m:02d}:{s:02d}"


def _write_txt(
    path: Path,
    segments: list[Segment],
    role_map: dict[str, str],
    *,
    header_lines: list[str],
) -> None:
    """寫一份逐字稿 .txt（含角色區塊、時間戳）。"""
    lines = list(header_lines) + [""]
    current_role = None
    for seg in segments:
        sid = seg.speaker or "UNKNOWN"
        role = role_map.get(sid, sid)
        if role != current_role:
            lines.append("")
            lines.append("=" * 40)
            lines.append(f"【{role}】")
            lines.append("=" * 40)
            current_role = role
        ts = f"[{_fmt_ts(seg.start)}-{_fmt_ts(seg.end)}]"
        lines.append(f"{ts} {seg.text.strip()}")
    path.write_text("\n".join(lines), encoding="utf-8")


def write_results(
    *,
    output_dir: Path,
    raw_segments: list[Segment],
    polished_segments: list[Segment] | None,
    role_map: dict[str, str],
    speaker_samples: dict[str, list[str]],
    job_meta: dict,
) -> None:
    """寫所有產出檔案到 output_dir。"""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    now_str = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")
    duration_sec = max((s.end for s in raw_segments), default=0)
    header_base = [
        f"# {job_meta.get('original_name', 'transcript')}",
        f"# 生成時間：{now_str}",
        f"# 時長：{_fmt_ts(duration_sec)}",
        f"# 會議類型：{job_meta.get('meeting_type', 'other')}",
        f"# Job ID：{job_meta.get('id', '-')}",
    ]

    # Raw 版（無 LLM 精修，OpenCC 繁體 + speaker 角色 + 時間戳）
    _write_txt(
        output_dir / "transcript.raw.txt",
        raw_segments,
        role_map,
        header_lines=header_base + ["# 版本：raw（無 LLM 精修）"],
    )

    # Polished 版（如果有跑 Step 7）
    if polished_segments:
        _write_txt(
            output_dir / "transcript.polished.txt",
            polished_segments,
            role_map,
            header_lines=header_base + ["# 版本：polished（Haiku 4.5 標點補齊）"],
        )

    # 結構化 segments.json
    with open(output_dir / "segments.json", "w", encoding="utf-8") as f:
        json.dump(
            {
                "raw": [s.to_dict() for s in raw_segments],
                "polished": [s.to_dict() for s in polished_segments] if polished_segments else None,
                "role_map": role_map,
            },
            f, ensure_ascii=False, indent=2, default=str,
        )

    # 角色判斷樣本（後續若 LLM 判錯，可手動參考重跑）
    with open(output_dir / "speakers.json", "w", encoding="utf-8") as f:
        json.dump(speaker_samples, f, ensure_ascii=False, indent=2)

    # Job metadata
    with open(output_dir / "meta.json", "w", encoding="utf-8") as f:
        json.dump(job_meta, f, ensure_ascii=False, indent=2, default=str)
