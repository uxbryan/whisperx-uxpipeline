"""
core/polish.py — Step 7: LLM 標點補齊

策略：
  1. 把 segments 切成 ~5 分鐘 chunk（在 speaker 切換點斷開以保整段語意）
  2. 平行送 Haiku 4.5（每次處理一個 chunk 內的多句文字）
  3. LLM 只負責補標點，不擴寫不修詞
  4. 用「編號標記」格式比對，缺漏的回退到原文

成本：估算 + 累計 NTD（待後續換用 SDK 拿真實 token 數）。
未來優化：Anthropic prompt cache，cache hit 收 1/10。
"""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

from .llm import call
from .pipeline import Segment

DEFAULT_MODEL = "anthropic/claude-haiku-4-5"

POLISH_PROMPT = """你的任務：把口語逐字稿補上完整的標點符號。

規則（嚴格遵守）：
1. 只補標點，不修改字詞、不擴寫、不刪減任何字。
2. 中文用全形標點（，。？！「」），英文/數字後可用半形。
3. 保留所有口語贅字（「然後…」「就是…」「那個…」）。
4. 輸入是編號清單，輸出**完全相同的編號**，順序不變。
5. 只輸出編號清單，不要任何說明文字。

輸入格式範例：
[1] 好請開始大家好我們的工作組是Campus Plus
[2] 就是因為大學生活形態改變的關係學生的需求不再只是學習

輸出格式範例：
[1] 好，請開始。大家好，我們的工作組是 Campus Plus。
[2] 就是因為大學生活形態改變的關係，學生的需求不再只是學習。

現在開始處理以下內容：

"""


@dataclass
class _Chunk:
    """一塊待 polish 的 segments（保持原 index 以便回填）。"""
    indices: list[int]            # segments 的全局 index
    segments: list[Segment]       # 對應的 segments 物件


def _split_chunks(
    segments: list[Segment], chunk_minutes: int = 5
) -> list[_Chunk]:
    """切 chunk：每塊約 chunk_minutes 分鐘，盡量在 speaker 切換點斷開。"""
    if not segments:
        return []

    chunks: list[_Chunk] = []
    cur_indices: list[int] = []
    cur_segs: list[Segment] = []
    cur_start = segments[0].start
    chunk_sec = chunk_minutes * 60
    last_speaker = None

    for i, seg in enumerate(segments):
        speaker_changed = (seg.speaker != last_speaker) and last_speaker is not None
        time_elapsed = seg.start - cur_start

        # 超過 chunk 長度，且發生 speaker 切換 → 斷
        if cur_segs and time_elapsed >= chunk_sec and speaker_changed:
            chunks.append(_Chunk(cur_indices, cur_segs))
            cur_indices, cur_segs = [], []
            cur_start = seg.start

        # 嚴重超長（2× chunk）也強制斷，避免單 chunk 太大
        elif cur_segs and time_elapsed >= chunk_sec * 2:
            chunks.append(_Chunk(cur_indices, cur_segs))
            cur_indices, cur_segs = [], []
            cur_start = seg.start

        cur_indices.append(i)
        cur_segs.append(seg)
        last_speaker = seg.speaker

    if cur_segs:
        chunks.append(_Chunk(cur_indices, cur_segs))
    return chunks


def _build_chunk_prompt(chunk: _Chunk) -> str:
    """組裝編號清單 prompt。"""
    lines = [POLISH_PROMPT]
    for local_i, seg in enumerate(chunk.segments, start=1):
        lines.append(f"[{local_i}] {seg.text.strip()}")
    return "\n".join(lines)


_LINE_RE = re.compile(r"^\s*\[(\d+)\]\s*(.+)$")


def _parse_chunk_response(response: str, chunk: _Chunk) -> dict[int, str]:
    """解析 LLM 回應，回傳 {local_index_1_based: polished_text}。"""
    out: dict[int, str] = {}
    for line in response.splitlines():
        m = _LINE_RE.match(line)
        if m:
            idx = int(m.group(1))
            text = m.group(2).strip()
            out[idx] = text
    return out


def _polish_chunk(chunk: _Chunk, model: str) -> tuple[dict[int, str], float]:
    """單一 chunk 的 LLM 呼叫。回傳 (local_idx → polished_text, cost_ntd)。"""
    prompt = _build_chunk_prompt(chunk)
    response, cost = call(model, prompt, source="voice-to-text.polish")
    return _parse_chunk_response(response, chunk), cost


def polish(
    segments: list[Segment],
    *,
    model: str = DEFAULT_MODEL,
    chunk_minutes: int = 5,
    parallel: int = 3,
    progress_cb: callable | None = None,
) -> tuple[list[Segment], float]:
    """主入口。回傳 (polished segments, 累計 NTD 成本)。

    原 segments 不被改動，回傳的是新物件清單。LLM 缺漏的段落會 fallback 到原文。
    """
    if not segments:
        return [], 0.0

    chunks = _split_chunks(segments, chunk_minutes)
    polished_text: dict[int, str] = {}  # global_index → text
    total_cost = 0.0
    done_count = 0

    with ThreadPoolExecutor(max_workers=parallel) as exe:
        futures = {exe.submit(_polish_chunk, c, model): c for c in chunks}
        for fut in as_completed(futures):
            chunk = futures[fut]
            try:
                local_map, cost = fut.result()
                total_cost += cost
                for local_i, text in local_map.items():
                    global_i = chunk.indices[local_i - 1]
                    polished_text[global_i] = text
            except Exception:
                # 整 chunk 失敗 → 該段全部 fallback 原文（下方迴圈會處理）
                pass
            done_count += 1
            if progress_cb:
                progress_cb("polishing", int(80 + 20 * done_count / len(chunks)))

    # 組回新的 segment 清單
    result: list[Segment] = []
    for i, seg in enumerate(segments):
        new_seg = Segment(
            start=seg.start,
            end=seg.end,
            text=polished_text.get(i, seg.text),  # 缺漏 fallback
            speaker=seg.speaker,
            words=list(seg.words),
        )
        result.append(new_seg)

    return result, total_cost
