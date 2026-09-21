"""Conservative discontinuity cues; never reconstruct speech or modify transcripts."""
from __future__ import annotations

import json
import re


GAP_INSTRUCTIONS = """recording_gaps 是录音中断的线索，不证明缺失了某句话。长停顿也可能来自板书、演示或课间休息。
仅在这里列出的连接处按两侧内容判断是否需要补充知识。必要时以“补充讲解：”简短连接标准定义或推导，
推测用“根据前后文，这里可能需要……”表达。补充文字只进入整理稿，不能写回原始转写或合成冒充老师的录音。"""

_MARKER = re.compile(r"(?:\[|【|<)(?:录音|音频|转写)(?:中断|缺失|缺段|断开|恢复)[^\]\n】>]{0,80}(?:\]|】|>)")


def analyze_recording_gaps(transcript: str, *, utterances: list[dict] | None = None,
                           recording_boundaries: list[dict] | None = None) -> list[dict]:
    """Use explicit markers or timed pauses between unfinished utterances.

    ASR start_time/end_time are milliseconds. Plain silence and ordinary file
    boundaries alone do not establish a gap. All excerpts are small input data.
    """
    cues = []
    for match in _MARKER.finditer(transcript):
        cues.append({"kind": "explicit_marker", "marker": match[0],
                     "before": transcript[max(0, match.start() - 160):match.start()],
                     "after": transcript[match.end():match.end() + 160]})
    previous = None
    for current in utterances or []:
        if not isinstance(current, dict):
            continue
        try:
            start, end = float(current["start_time"]), float(current["end_time"])
        except (KeyError, TypeError, ValueError):
            continue
        if not (0 <= start <= end < 24 * 3600 * 1000):
            continue
        text = str(current.get("text") or "").strip()
        if previous and start - previous["end"] >= 90000 and _unfinished(previous["text"]) and text:
            cues.append({"kind": "pause_after_unfinished_sentence", "startSeconds": previous["end"] / 1000,
                         "endSeconds": start / 1000, "before": previous["text"][-160:], "after": text[:160]})
        previous = {"end": end, "text": text}
    for boundary in recording_boundaries or []:
        if not isinstance(boundary, dict):
            continue
        before, after = str(boundary.get("before") or ""), str(boundary.get("after") or "")
        if _unfinished(before) and after.strip():
            cues.append({"kind": "unfinished_file_boundary", "before": before[-160:], "after": after[:160]})
    return cues[:8]


def _unfinished(text: str) -> bool:
    return bool(re.search(r"(?:所以|因为|那么|也就是|等于|代入|可以得到|接下来是|分为|分别是|：|:|，|,)$", text.strip()))


def recording_gap_context(transcript: str, *, utterances: list[dict] | None = None,
                          recording_boundaries: list[dict] | None = None) -> str:
    cues = analyze_recording_gaps(transcript, utterances=utterances, recording_boundaries=recording_boundaries)
    if not cues:
        return ""
    payload = json.dumps(cues, ensure_ascii=False).replace("<", "\\u003c").replace(">", "\\u003e")
    return "\n\n<recording_gaps>\n" + payload + "\n</recording_gaps>"
