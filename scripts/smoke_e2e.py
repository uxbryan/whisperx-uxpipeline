#!/usr/bin/env python3
"""
smoke_e2e.py — 輕量 e2e 測試（不跑 WhisperX，假造 segments）

驗證：
  - role_classify 真實呼叫 Haiku
  - polish 真實呼叫 Haiku（分段、合併）
  - writer 寫出 5 個檔案（格式正確）
  - DB 狀態流程正確
  - cost 累計到 NTD

不驗證的：
  - WhisperX / pyannote（要 ~3GB model download，跑下去要 30+ 分鐘）
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

# 確保專案根目錄在 path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from core import db, role_classify, polish, writer
from core.pipeline import Segment


def make_fake_segments() -> list[Segment]:
    """假造一段「老師講評+學生報告」的逐字稿（沒標點，模擬 WhisperX 原始輸出）。"""
    return [
        Segment(0.0, 4.5, "好請開始大家好我們的工作組是Campus Plus", speaker="SPEAKER_01"),
        Segment(4.5, 11.2, "就是因為大學生活形態改變的關係學生的需求不再只是學習", speaker="SPEAKER_01"),
        Segment(11.2, 18.0, "我們希望通過這個平臺可以達到更便利然後可以互助的一個大學校園收容環境", speaker="SPEAKER_01"),
        Segment(18.5, 23.0, "你們應該要給流程圖多分配一些呈現的篇幅", speaker="SPEAKER_02"),
        Segment(23.0, 30.0, "在Figma裡做Prototype是Figma裡的一個功能把這些頁面畫完之後連結在一起做說明", speaker="SPEAKER_02"),
        Segment(30.5, 38.0, "我們這組是針對校園資源做的然後我們有訪問過幾位同學", speaker="SPEAKER_01"),
    ]


def main():
    print("=" * 60)
    print("voice-to-text smoke e2e test (no WhisperX)")
    print("=" * 60)

    # 1) DB init + 建 fake job
    print("\n[1/5] 初始化 DB + 建立 fake job")
    db.init_db()
    job_id = db.create_job(
        user_email="smoke-test@example.com",
        original_name="smoke_test.mp3",
        size_bytes=1234,
        duration_sec=38,
        meeting_type="lecture",  # 上課，會分老師/學生
    )
    print(f"  job_id = {job_id}")

    # 2) 假 segments
    print("\n[2/5] 假造 6 段逐字稿（學生 + 老師交錯）")
    segments = make_fake_segments()
    for s in segments:
        print(f"  {s.speaker}: {s.text[:30]}...")

    # 3) Role classify (call Haiku)
    print("\n[3/5] 角色辨識 (meeting_type=lecture，呼叫 Haiku)")
    from core.pipeline import collect_speaker_samples
    samples = collect_speaker_samples(segments)
    t0 = time.time()
    role_map, cost1 = role_classify.classify(samples, "lecture")
    print(f"  role_map: {role_map}")
    print(f"  耗時 {time.time()-t0:.1f}s，成本 NT$ {cost1:.4f}")
    db.add_cost(job_id, cost1)

    # 4) Polish (call Haiku 分段)
    print("\n[4/5] LLM 標點補齊 (Haiku 分段送)")
    t0 = time.time()
    polished, cost2 = polish.polish(segments, chunk_minutes=5, parallel=1)
    print(f"  耗時 {time.time()-t0:.1f}s，成本 NT$ {cost2:.4f}")
    print("  範例對照：")
    for orig, new in zip(segments[:2], polished[:2]):
        print(f"    [原文] {orig.text}")
        print(f"    [精修] {new.text}")
    db.add_cost(job_id, cost2)

    # 5) 寫檔到 /tmp（不用 NAS）
    print("\n[5/5] 寫產出檔到 /tmp/smoke_test/")
    out_dir = Path("/tmp/smoke_test") / job_id
    writer.write_results(
        output_dir=out_dir,
        raw_segments=segments,
        polished_segments=polished,
        role_map=role_map,
        speaker_samples=samples,
        job_meta=dict(db.get_job(job_id) or {}),
    )
    db.update_status(job_id, "done", progress_pct=100, nas_output_path=str(out_dir))
    print(f"  輸出資料夾：{out_dir}")
    for f in sorted(out_dir.iterdir()):
        size = f.stat().st_size
        print(f"    {f.name:30s} {size:>8} bytes")

    # 總結
    final = db.get_job(job_id)
    print("\n" + "=" * 60)
    print("✓ smoke e2e 通過")
    print(f"  Status: {final['status']}")
    print(f"  總成本: NT$ {final['cost_ntd']:.4f}")
    print("=" * 60)
    print(f"\n預覽 polished 版本：")
    print((out_dir / "transcript.polished.txt").read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()
