"""Local timetable extraction, semantic abbreviations, and subject vocabulary.

No external model call or charge is made. Images use Chinese OCR coordinates;
PDF text keeps its coordinates and scanned pages use the same OCR path. Excel
uses real cells, including merges. A preview must be accepted before persistence.
"""
from __future__ import annotations

import csv
import hashlib
import io
import math
import os
import re
import shutil
import statistics
import subprocess
import tempfile
import zipfile
from collections import defaultdict
from contextlib import closing
from datetime import date
from pathlib import Path

MAX_FILE_BYTES = 3 * 1024 * 1024  # base64 remains below Vercel's request limit
MAX_PDF_PAGES = 10
MAX_PIXELS = 18_000_000
EXTRACTOR_VERSION = "v3-grid-4"
COLORS = ("#A9C5B4", "#B8C9DB", "#D2BDD9", "#E1C4A8", "#DFC0BD", "#C4CFA7", "#ADCBD0", "#D7CDAB")
DAY = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "日": 7, "天": 7}
CN_NUM = {"一": "1", "二": "2", "三": "3", "四": "4", "五": "5", "六": "6", "七": "7", "八": "8", "九": "9", "十": "10"}
DAY_RE = re.compile(r"(?:星期|周|礼拜)([一二三四五六日天1-7])|\b(Mon(?:day)?|Tue(?:sday)?|Wed(?:nesday)?|Thu(?:rsday)?|Fri(?:day)?|Sat(?:urday)?|Sun(?:day)?)\b", re.I)

SUBJECTS = (
    (("机器人", "robot"), "雅可比矩阵,雅克比矩阵,正运动学,逆运动学,DH参数,齐次变换,关节空间,笛卡尔空间,自由度,轨迹规划"),
    (("驱动", "电机", "执行器"), "伺服电机,步进电机,减速器,谐波减速器,力矩,转矩,编码器,执行器,液压驱动,气动肌肉,形状记忆合金,压电,介电弹性体"),
    (("控制", "建模"), "状态空间,传递函数,反馈控制,PID控制,稳定性,可控性,可观性,拉普拉斯变换,奈奎斯特,根轨迹,拉格朗日方程"),
    (("制造", "机械加工"), "切削加工,车削,铣削,磨削,铸造,锻造,切削力,刀具,表面粗糙度,切削用量,数控加工"),
    (("机械", "力学"), "应力,应变,弯矩,惯性矩,刚度,阻尼,固有频率,疲劳,有限元"),
    (("数学", "微积分", "高数"), "极限,连续,导数,微分,定积分,泰勒展开,级数,偏导数,梯度,拉格朗日乘数法"),
    (("线性代数", "线代"), "特征值,特征向量,矩阵,行列式,秩,线性空间,正交,奇异值分解"),
    (("概率", "统计"), "随机变量,概率密度,条件概率,贝叶斯,期望,方差,协方差,大数定律,中心极限定理"),
    (("电路", "电子"), "基尔霍夫,戴维南,运算放大器,晶体管,阻抗,电感,电容,频率响应"),
    (("计算机", "程序", "算法", "数据结构"), "时间复杂度,递归,哈希表,二叉树,动态规划,堆栈,指针,线程,进程"),
    (("环境工程",), "物料衡算,传质,传热,流体输送,沉降,过滤,污染物,污水处理,质量守恒"),
    (("物理",), "动量,角动量,能量守恒,电磁场,简谐振动,干涉,衍射,量子态"),
    (("毛泽东", "毛概"), "新民主主义,马克思主义中国化,社会主义改造,邓小平理论,三个代表,科学发展观,实事求是"),
    (("英语", "eap", "academic"), "academic writing,topic sentence,thesis statement,paraphrasing,literature review,academic presentation"),
)


def course_hotwords(name: str) -> str:
    words = [name.strip()]
    for keys, values in SUBJECTS:
        if any(key in name.lower() for key in keys):
            words.extend(values.split(","))
    return ",".join(dict.fromkeys(word for word in words if word))


def short_name(name: str, used=()) -> str:
    """Prefer distinctive subject tokens, extending only when they collide."""
    clean = re.sub(r"\s+", "", name)
    aliases = (("建模与控制", "建控"), ("驱动系统", "驱动"), ("机械制造基础", "机制"),
               ("制造工程认知实践", "制造实践"), ("毛泽东思想", "毛概"), ("线性代数", "线代"),
               ("高等数学", "高数"), ("概率论", "概率"), ("环境工程原理", "环工"),
               ("数据结构", "数据结构"), ("自动控制", "自控"))
    candidates = [value for key, value in aliases if key in clean]
    if re.search(r"[（(【]实验[）)】]", clean):
        candidates = [value.removesuffix("实践") + "实验" for value in candidates] + candidates
    stem = re.sub(r"(?:机器人|课程|基础|原理|概论|导论|系统|理论|大学)", "", clean)
    if "与" in stem or "及" in stem:
        parts = re.split("[与及]", stem)
        candidates.append("".join(x[0] for x in parts if x))
    candidates.extend([stem[:4], clean[:4]])
    occupied = {x.casefold() for x in used}
    for candidate in candidates:
        if candidate and candidate.casefold() not in occupied:
            return candidate
    # Keep a meaningful four-character stem plus a deterministic name fragment.
    for length in range(5, min(len(clean), 10) + 1):
        candidate = clean[:length]
        if candidate.casefold() not in occupied:
            return candidate
    stem = (candidates[0] or clean)[:8]
    counter = 2
    while f"{stem}{counter}".casefold() in occupied:
        counter += 1
    return f"{stem}{counter}"


def parse_weeks(text: str) -> tuple[list[int], bool]:
    text = re.sub(r"\s+", "", text).replace("（", "(").replace("）", ")")
    match = re.search(r"(?:第)?(\d{1,2}(?:(?:[-—–~至,，、])\d{1,2})*)\s*(?:\([单双]\)|[单双])?周", text)
    weeks = set()
    if match:
        for part in re.split("[,，、]", match.group(1)):
            values = re.split("[-—–~至]", part)
            first, last = int(values[0]), int(values[-1])
            if not 1 <= first <= last <= 20:
                raise ValueError("课表周次需要在第 1–20 周内。")
            weeks.update(range(first, last + 1))
    else:
        weeks.update(range(1, 21))
    parity = 1 if re.search(r"单周|周\(?单\)?|\(单\)周", text) else 0 if re.search(r"双周|周\(?双\)?|\(双\)周", text) else None
    return sorted(w for w in weeks if parity is None or w % 2 == parity), bool(match)


def parse_periods(text: str) -> list[int]:
    text = re.sub(r"\s+", "", text)
    text = text.replace("～", "~").replace("－", "-").replace("﹣", "-")
    text = re.sub(r"(?<!\d)0([1-9])", r"\1", text)
    text = re.sub(r"^(\d+)\.0$", r"\1", text).strip("()（）[]【】")
    for cn, number in CN_NUM.items():
        text = text.replace(cn, number)
    match = re.search(r"(?<!\d)(?:第)?(10|[1-9])(?:[-—–~至、,，](10|[1-9]))?节", text)
    if not match:
        match = re.fullmatch(r"(?:第)?(10|[1-9])(?:[-—–~至、,，](10|[1-9]))?", text)
    if not match:
        return []
    first, last = int(match.group(1)), int(match.group(2) or match.group(1))
    return sorted({(x + 1) // 2 for x in range(first, last + 1)}) if first <= last <= 10 else []


def _weekday(text: str) -> int | None:
    clean = re.sub(r"\s+", "", text).strip("()（）[]【】")
    if clean in DAY:
        return DAY[clean]
    match = DAY_RE.search(clean.replace("星期—", "星期一").replace("周—", "周一"))
    if not match:
        return None
    if match.group(1):
        return DAY.get(match.group(1)) or int(match.group(1))
    return ["mon", "tue", "wed", "thu", "fri", "sat", "sun"].index(match.group(2)[:3].lower()) + 1


def _name(text: str) -> str:
    lines = [re.sub(r"\s+", "", x).strip(" |：:·") for x in text.splitlines() if x.strip()]
    # Portal cards put the wrapped title before bracketed teacher/class metadata.
    # Keep the entire title, including a separate (实验) line: lecture and lab
    # are different courses. Never join the teacher or repeated class name.
    metadata = next((i for i, line in enumerate(lines) if re.match(r"^[\[【](?!实验[】\]])", line)), None)
    if metadata and metadata <= 5:
        title = "".join(lines[:metadata]).replace("(", "（").replace(")", "）").replace("【实验】", "（实验）")
        if 2 <= len(title) <= 120 and not re.search(r"\d+(?:[-~]\d+)?[单双]?周|\d+节", title):
            return title
    result = []
    for line in lines:
        line = re.sub(r"^(?:课程名称|课程)[:：]", "", line)
        # Course codes, teachers, week numbers and rooms are metadata.
        if re.fullmatch(r"[A-Z]{1,6}\d{2,}[A-Z\d-]*", line) or re.match(r"^(?:教师|老师|教室|地点|学分|班级|周次|节次)[:：]", line):
            continue
        prefix = re.split(r"(?:第?\d{1,2}[-—–~至]\d{1,2}(?:\([单双]\))?周|第?\d{1,2}周|[（(](?:单|双)周|(?:星期|周)[一二三四五六日]|\d{1,2}[-—–~至、,，]\d{1,2}节)", line)[0]
        prefix = prefix.strip(" -/·")
        if not prefix or re.fullmatch(r"[\d\W]+", prefix):
            break
        if len(prefix) >= 2:
            result.append(prefix)
        # A name is usually the first line; join a wrapped continuation only if
        # it contains a subject suffix, never blindly append a teacher's name.
        if result:
            if len(lines) > 1 and lines[0] == line and len(prefix) <= 8:
                nxt = lines[1]
                if re.fullmatch(r"[\u4e00-\u9fff]{0,8}(?:系统|控制|基础|原理|实践|概论)", nxt):
                    result.append(nxt)
            break
    name = "".join(result).strip()
    if not 2 <= len(name) <= 120 or name in {"上午", "下午", "晚上", "节次", "时间", "课程表", "课表", "午休"}:
        return ""
    return name


def _new_preview() -> dict:
    return {"semesterStart": None, "weeks": 20, "courses": [], "slots": []}


def _add_cell(preview: dict, text: str, weekday: int, periods: list[int], warnings: list[str]):
    name = _name(text)
    if not name or not periods:
        return
    weeks, _ = parse_weeks(text)
    course = next((x for x in preview["courses"] if x["name"] == name), None)
    if not course:
        course = {"key": hashlib.sha256(name.encode()).hexdigest()[:16], "name": name,
                  "shortName": short_name(name, [c["shortName"] for c in preview["courses"]]),
                  "color": COLORS[len(preview["courses"]) % len(COLORS)], "hotwords": course_hotwords(name)}
        preview["courses"].append(course)
    for period in periods:
        slot = next((x for x in preview["slots"] if x["courseKey"] == course["key"] and x["weekday"] == weekday and x["period"] == period), None)
        if slot:
            slot["weeks"] = sorted(set(slot["weeks"]) | set(weeks))
        else:
            preview["slots"].append({"courseKey": course["key"], "weekday": weekday, "period": period,
                                     "weeks": weeks, "location": ""})


def parse_table(rows: list[list[str]], *, spans: dict | None = None) -> tuple[dict, list[str]]:
    """Read a weekday-column table, or an explicit course/day/period row list."""
    preview, warnings = _new_preview(), []
    spans = spans or {}
    header = next(((i, {j: _weekday(str(v)) for j, v in enumerate(row) if _weekday(str(v))})
                   for i, row in enumerate(rows) if sum(_weekday(str(v)) is not None for v in row) >= 3), None)
    if header:
        hrow, columns = header
        left = min(columns)
        row_periods = {}
        for i in range(hrow + 1, len(rows)):
            labels = [p for text in rows[i][:left] for p in parse_periods(str(text))]
            if labels:
                row_periods[i] = sorted(set(labels))
        # A merged period label is common in exported teaching-system tables.
        for (r, c), (height, _width) in spans.items():
            if c < left and r in row_periods:
                for i in range(r + 1, r + height):
                    row_periods.setdefault(i, row_periods[r])
        if not row_periods:
            warnings.append("未识别出节次，请使用含一至十节标记的完整课表。")
        for i, periods in row_periods.items():
            for j, day in columns.items():
                text = str(rows[i][j]) if j < len(rows[i]) else ""
                if not text.strip():
                    continue
                span_rows, span_cols = spans.get((i, j), (1, 1))
                merged_periods = sorted({p for r in range(i, i + span_rows) for p in row_periods.get(r, periods)})
                for col in range(j, j + span_cols):
                    if col in columns:
                        _add_cell(preview, text, columns[col], merged_periods, warnings)
    else:
        for row in rows:
            text = " ".join(str(x) for x in row)
            day, periods = _weekday(text), parse_periods(text)
            if day and periods:
                _add_cell(preview, text, day, periods, warnings)
    raw = "\n".join(" ".join(map(str, row)) for row in rows)
    _extract_start(raw, preview)
    return preview, warnings


def _extract_start(text: str, preview: dict):
    match = re.search(r"(?:第一周周一|第1周周一|学期开始|开学日期|教学周开始)\s*[:：]?\s*(20\d{2})[-年/.](\d{1,2})[-月/.](\d{1,2})", text)
    if match:
        try:
            value = date(*map(int, match.groups()))
            if value.weekday() == 0:
                preview["semesterStart"] = value.isoformat()
        except ValueError:
            pass


def _lines(words: list[dict]) -> list[list[dict]]:
    lines = []
    for word in sorted(words, key=lambda w: (w["y"], w["x"])):
        center = word["y"] + word["h"] / 2
        target = next((line for line in reversed(lines[-6:]) if abs(center - statistics.mean(x["y"] + x["h"] / 2 for x in line)) < max(3, word["h"] * .55)), None)
        if target is None:
            lines.append([word])
        else:
            target.append(word)
    return [sorted(line, key=lambda w: w["x"]) for line in lines]


def _line_text(line: list[dict]) -> str:
    result = ""
    previous = None
    for word in line:
        if previous and word["x"] - previous["x"] - previous["w"] > max(8, word["h"] * .7):
            result += " "
        result += word["text"]
        previous = word
    return result


def _row_rules(image, left: float, right: float, top: float, bottom: float) -> list[float]:
    """Read visible horizontal cell rules, including boundaries of merged cells."""
    if image is None or right - left < 15:
        return []
    x0, x1 = max(0, round(left + 4)), min(image.width, round(right - 4))
    y0, y1 = max(0, round(top)), min(image.height, round(bottom))
    if x1 <= x0 or y1 <= y0:
        return []
    strip = image.crop((x0, y0, x1, y1)).convert("L")
    strip = strip.resize((100, strip.height))
    values = list(strip.getdata())
    candidates = [y for y in range(strip.height) if sum(v < 210 for v in values[y * 100:(y + 1) * 100]) >= 78]
    runs = []
    for y in candidates:
        if runs and y == runs[-1][-1] + 1:
            runs[-1].append(y)
        else:
            runs.append([y])
    # Solid coloured blocks and photographs are not table rules.
    return [y0 + statistics.mean(run) for run in runs if len(run) <= max(6, image.height * .007)]


def _headers(lines: list[list[dict]]) -> list[tuple]:
    for line in lines:
        found = []
        # English weekday boundaries and isolated Chinese headings must survive
        # joining; old code turned 'Mon Tue Wed' into 'MonTueWed'.
        for word in line:
            day = _weekday(word["text"])
            if day and len(word["text"].strip()) <= 12:
                found.append((day, word["x"] + word["w"] / 2, word["y"] + word["h"]))
        token_headers = found
        found = []
        chars, refs = "", []
        for word in line:
            token = word["text"].replace(" ", "")
            chars += token
            # OCR may return several headings in one token. Each character
            # needs its own horizontal interval, otherwise all days collapse
            # onto the center of the same token and column spacing becomes zero.
            width = word["w"] / max(1, len(token))
            refs.extend({**word, "x": word["x"] + i * width, "w": width} for i in range(len(token)))
        for match in DAY_RE.finditer(chars):
            boxes = refs[match.start():match.end()]
            x0, x1 = min(w["x"] for w in boxes), max(w["x"] + w["w"] for w in boxes)
            found.append((_weekday(match.group()), (x0 + x1) / 2, max(w["y"] + w["h"] for w in boxes)))
        if len({h[0] for h in found}) >= 3:
            return list({h[0]: h for h in found}.values())
        if len({h[0] for h in token_headers}) >= 3:
            return token_headers
    return []


def _period_labels(words: list[dict]) -> dict[int, list[float]]:
    labels = defaultdict(list)
    for line in _lines(words):
        periods = parse_periods(_line_text(line))
        if not periods:
            # A row can include a day-part heading, one period digit and a clock.
            periods = [p for word in line for token in word["text"].split() for p in parse_periods(token)]
        for period in set(periods):
            labels[period].append(statistics.mean(w["y"] + w["h"] / 2 for w in line))
    return labels


def parse_words(words: list[dict], *, image=None) -> tuple[dict, list[str]]:
    """Keep weekday columns separate; use drawn cell boundaries for merged rows."""
    preview, warnings = _new_preview(), []
    lines = _lines(words)
    headers = _headers(lines)
    if len(headers) < 3:
        return parse_table([[_line_text(line)] for line in lines])
    headers.sort(key=lambda x: x[1])
    distances = [(b[1] - a[1]) / (b[0] - a[0]) for a, b in zip(headers, headers[1:]) if b[0] > a[0]]
    spacing = statistics.median(distances) if distances else 0
    if spacing <= 0:
        return preview, ["星期列顺序不清楚，请重新选择完整课表。"]
    origin = statistics.mean(x - (day - 1) * spacing for day, x, _ in headers)
    left, top = origin - spacing / 2, max(h[2] for h in headers)
    axis_words = [w for w in words if w["x"] + w["w"] / 2 < left and w["y"] > top]
    labels = _period_labels(axis_words)
    if len(labels) < 2:
        # Cropped/partial timetables can still carry explicit periods in cells.
        for day, center, _ in headers:
            column = [w for w in words if center - spacing / 2 <= w["x"] + w["w"] / 2 < center + spacing / 2 and w["y"] > top]
            text = "\n".join(_line_text(line) for line in _lines(column))
            periods = parse_periods(text)
            if periods:
                _add_cell(preview, text, day, periods, warnings)
        if not preview["slots"]:
            warnings.append("节次没有读清，可在课表预览中点格子补上课程。")
        return preview, warnings
    centers = {p: statistics.mean(values) for p, values in labels.items()}
    ordered = sorted(centers.items())
    gap = statistics.median((yb - ya) / (pb - pa) for (pa, ya), (pb, yb) in zip(ordered, ordered[1:]))
    if gap <= 0:
        return preview, ["节次顺序不清楚，请重新选择完整课表。"]
    anchor = statistics.mean(y - (p - 1) * gap for p, y in ordered)
    centers = {p: centers.get(p, anchor + (p - 1) * gap) for p in range(1, 6)}
    for day in range(1, 8):
        x0, x1 = left + (day - 1) * spacing, left + day * spacing
        column = [w for w in words if x0 <= w["x"] + w["w"] / 2 < x1 and top < w["y"] + w["h"] / 2 <= centers[5] + gap * .55]
        rules = _row_rules(image, x0, x1, top, centers[5] + gap * .65)
        use_rules = len(rules) >= 2 and rules[0] < centers[1] and rules[-1] > centers[5]
        groups = defaultdict(list)
        for word in column:
            y = word["y"] + word["h"] / 2
            if use_rules:
                band = next((i for i in range(len(rules) - 1) if rules[i] <= y < rules[i + 1]), None)
                if band is not None:
                    groups[band].append(word)
            else:
                groups[min(centers, key=lambda p: abs(centers[p] - y))].append(word)
        for band, cell_words in sorted(groups.items()):
            text = "\n".join(_line_text(line) for line in _lines(cell_words))
            if use_rules:
                periods = sorted({p for p, ys in labels.items() if any(rules[band] <= y < rules[band + 1] for y in ys)})
                if not periods:
                    periods = [min(centers, key=lambda p: abs(centers[p] - (rules[band] + rules[band + 1]) / 2))]
            else:
                periods = [band]
            # Explicit cell metadata overrides geometric approximation.
            _add_cell(preview, text, day, parse_periods(text) or periods, warnings)
    _extract_start("\n".join(_line_text(line) for line in lines), preview)
    return preview, warnings


def _ocr(image, *, psm: int = 11) -> list[dict]:
    executable = os.environ.get("TESSERACT_CMD") or shutil.which("tesseract")
    if not executable and Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe").is_file():
        executable = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
    if not executable:
        raise ValueError("图片识别服务尚未就绪，请联系开发者。")
    from PIL import ImageOps
    image = ImageOps.exif_transpose(image).convert("RGB")
    if image.width * image.height > MAX_PIXELS:
        raise ValueError("图片像素过大，请压缩后导入。")
    scale = 1
    if image.width < 1800:
        scale = min(2, 1800 / image.width)
        image = image.resize((round(image.width * scale), round(image.height * scale)))
    with tempfile.TemporaryDirectory(prefix="tingji-timetable-") as folder:
        path = Path(folder) / "page.png"
        image.save(path)
        try:
            result = subprocess.run([executable, str(path), "stdout", "-l", "chi_sim+eng", "--psm", str(psm), "tsv"],
                                    capture_output=True, timeout=40, check=False,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ValueError("图片识别超时，请换用清晰的课表截图。") from exc
    if result.returncode:
        raise ValueError("图片识别失败，请换用清晰的课表截图。")
    words = []
    for row in csv.DictReader(io.StringIO(result.stdout.decode("utf-8", errors="replace")), delimiter="\t"):
        if row.get("level") != "5" or not row.get("text", "").strip():
            continue
        words.append({"text": row["text"].strip(), "x": float(row["left"]) / scale, "y": float(row["top"]) / scale,
                      "w": float(row["width"]) / scale, "h": float(row["height"]) / scale})
    return words



def _tesseract_words(image) -> list[dict]:
    """Read sparse course blocks, then recover single-character axis labels.

    PSM 11 intentionally ignores many isolated one-character regions. University
    timetables commonly use exactly those regions for periods 1 through 9, so a
    second, narrow PSM 6 pass is needed even when every course name was readable.
    Its coordinates are shifted back into the original image before parsing.
    """
    words = _ocr(image)
    headers = sorted(_headers(_lines(words)), key=lambda h: h[1])
    if len(headers) < 3:
        if parse_words(words)[0]["slots"]:
            return words
        # PSM 6 retains spaced weekday headings that sparse segmentation drops.
        alternative = _ocr(image, psm=6)
        if len(_headers(_lines(alternative))) >= 3:
            words, headers = alternative, sorted(_headers(_lines(alternative)), key=lambda h: h[1])
        else:
            return words
    distances = [(b[1] - a[1]) / (b[0] - a[0]) for a, b in zip(headers, headers[1:]) if b[0] > a[0]]
    spacing = statistics.median(distances) if distances else 0
    origin = statistics.mean(x - (day - 1) * spacing for day, x, _ in headers)
    left, top = round(origin - spacing / 2), max(h[2] for h in headers)
    if spacing <= 0 or left < 12 or left >= image.width:
        return words
    axis = [w for w in words if w["x"] + w["w"] / 2 < left and w["y"] > top]
    labels = _period_labels(axis)
    if len(labels) >= 5:
        return words
    # Skip the heading and retain a small margin so table borders are not tight
    # against glyphs. This crop is bounded by the detected Monday column.
    y0 = max(0, int(top))
    recovered = _ocr(image.crop((0, y0, left, image.height)), psm=6)
    recovered = [{**w, "y": w["y"] + y0} for w in recovered]
    if len(_period_labels(recovered)) <= len(labels):
        return words
    return [w for w in words if not (w["x"] + w["w"] / 2 < left and w["y"] > top)] + recovered


def _image_words(image) -> list[dict]:
    from .image_text import read_words
    return read_words(image)


def _parse_image(image) -> tuple[dict, list[str]]:
    from .image_text import colored_blocks
    words = _image_words(image)
    headers = sorted(_headers(_lines(words)), key=lambda h: h[1])
    blocks = colored_blocks(image)
    if len(headers) < 3 or not blocks:
        return parse_words(words, image=image)
    distances = [(b[1] - a[1]) / (b[0] - a[0]) for a, b in zip(headers, headers[1:]) if b[0] > a[0]]
    spacing = statistics.median(distances) if distances else 0
    if spacing <= 0:
        return parse_words(words, image=image)
    origin = statistics.mean(x - (day - 1) * spacing for day, x, _ in headers)
    top = max(h[2] for h in headers)
    labels = _period_labels([w for w in words if w["x"] + w["w"] / 2 < origin - spacing / 2 and w["y"] > top])
    preview, warnings = _new_preview(), []
    for x0, y0, x1, y1 in sorted(blocks, key=lambda b: (b[1], b[0])):
        center = (x0 + x1) / 2
        day = round((center - origin) / spacing) + 1
        if not 1 <= day <= 7 or y0 <= top or not spacing * .45 <= x1 - x0 <= spacing * 1.2:
            continue
        cell = [w for w in words if x0 <= w["x"] + w["w"] / 2 <= x1 and y0 <= w["y"] + w["h"] / 2 <= y1]
        text = "\n".join(_line_text(line) for line in _lines(cell))
        periods = parse_periods(text) or sorted(p for p, ys in labels.items() if any(y0 <= y <= y1 for y in ys))
        name = _name(text)
        if not name or not periods:
            warnings.append("有课程块未读清，请在预览中补充。")
            continue
        if not parse_weeks(text)[1]:
            warnings.append(f"「{name}」的周次未读清，请在预览中修改。")
        _add_cell(preview, text, day, periods, warnings)
    if not preview["slots"]:
        return parse_words(words, image=image)
    _extract_start("\n".join(w["text"] for w in words), preview)
    return preview, warnings


def _merge_preview(target: dict, incoming: dict):
    for course in incoming["courses"]:
        if not any(c["key"] == course["key"] for c in target["courses"]):
            course["shortName"] = short_name(course["name"], [c["shortName"] for c in target["courses"]])
            course["color"] = COLORS[len(target["courses"]) % len(COLORS)]
            target["courses"].append(course)
    for slot in incoming["slots"]:
        match = next((s for s in target["slots"] if (s["courseKey"], s["weekday"], s["period"]) == (slot["courseKey"], slot["weekday"], slot["period"])), None)
        if match:
            match["weeks"] = sorted(set(match["weeks"]) | set(slot["weeks"]))
        else:
            target["slots"].append(slot)
    if incoming["semesterStart"]:
        target["semesterStart"] = incoming["semesterStart"]


def extract_file(filename: str, content: bytes) -> dict:
    if not content or len(content) > MAX_FILE_BYTES:
        raise ValueError("课表文件需要非空，且不超过 3 MB。")
    suffix = Path(filename).suffix.lower()
    preview, warnings = _new_preview(), []
    method = ""
    try:
        if suffix in {".png", ".jpg", ".jpeg", ".webp", ".bmp"}:
            from PIL import Image, ImageOps
            with Image.open(io.BytesIO(content)) as source:
                if source.width * source.height > MAX_PIXELS:
                    raise ValueError("图片像素过大，请压缩后导入。")
                image = ImageOps.exif_transpose(source).convert("RGB")
                preview, warnings = _parse_image(image)
            method = "rapidocr-grid"
        elif suffix in {".xlsx", ".xls"}:
            sheets = []
            if suffix == ".xlsx":
                with zipfile.ZipFile(io.BytesIO(content)) as archive:
                    if len(archive.infolist()) > 1000 or sum(x.file_size for x in archive.infolist()) > 32 * 1024 * 1024:
                        raise ValueError("Excel 内容过大，请仅保留课表工作表。")
                import openpyxl
                book = openpyxl.load_workbook(io.BytesIO(content), data_only=True, keep_links=False)
                try:
                    if len(book.worksheets) > 10:
                        raise ValueError("Excel 最多导入 10 张工作表。")
                    for sheet in book.worksheets:
                        if sheet.max_row > 500 or sheet.max_column > 60:
                            raise ValueError("Excel 课表最多 500 行、60 列，请仅保留课表区域。")
                        rows = [[str(v) if v is not None else "" for v in row] for row in sheet.iter_rows(values_only=True)]
                        spans = {(r.min_row - 1, r.min_col - 1): (r.max_row - r.min_row + 1, r.max_col - r.min_col + 1) for r in sheet.merged_cells.ranges}
                        sheets.append((rows, spans))
                finally:
                    book.close()
            else:
                import xlrd
                book = xlrd.open_workbook(file_contents=content, formatting_info=True, logfile=io.StringIO())
                try:
                    if book.nsheets > 10:
                        raise ValueError("Excel 最多导入 10 张工作表。")
                    for sheet in book.sheets():
                        if sheet.nrows > 500 or sheet.ncols > 60:
                            raise ValueError("Excel 课表最多 500 行、60 列，请仅保留课表区域。")
                        rows = [[str(x) if x != "" else "" for x in sheet.row_values(i)] for i in range(sheet.nrows)]
                        spans = {(a, c): (b - a, d - c) for a, b, c, d in sheet.merged_cells}
                        sheets.append((rows, spans))
                finally:
                    book.release_resources()
            for rows, spans in sheets:
                part, notices = parse_table(rows, spans=spans)
                _merge_preview(preview, part)
                warnings.extend(notices)
            method = "excel-cells"
        elif suffix == ".pdf":
            import pypdfium2 as pdfium
            with pdfium.PdfDocument(content) as document:
                if not 1 <= len(document) <= MAX_PDF_PAGES:
                    raise ValueError("PDF 最多导入 10 页。")
                ocr_pages = 0
                for page in document:
                    with closing(page.get_textpage()) as textpage:
                        words = []
                        if textpage.count_chars() > 12000:
                            raise ValueError("PDF 文字过多，请仅保留课表页面。")
                        for index in range(textpage.count_chars()):
                            char = textpage.get_text_range(index, 1)
                            if not char.strip():
                                continue
                            left, bottom, right, top = textpage.get_charbox(index)
                            words.append({"text": char, "x": left, "y": page.get_height() - top,
                                          "w": right - left, "h": top - bottom})
                    scale = min(2.5, (MAX_PIXELS / max(1, page.get_width() * page.get_height())) ** .5)
                    bitmap = page.render(scale=scale)
                    try:
                        image = bitmap.to_pil()
                        scaled_words = [{**w, **{axis: w[axis] * scale for axis in ("x", "y", "w", "h")}} for w in words]
                        part, notices = parse_words(scaled_words, image=image)
                        needs_ocr = not part["slots"]
                        if needs_ocr:
                            ocr_pages += 1
                            if ocr_pages > 3:
                                raise ValueError("扫描 PDF 每次最多识别 3 页，请拆分后导入。")
                            part, notices = _parse_image(image)
                    finally:
                        bitmap.close()
                    _merge_preview(preview, part)
                    warnings.extend(notices)
                    page.close()
            method = "pdf-text+ocr" if ocr_pages else "pdf-text"
        else:
            raise ValueError("请选择课表图片、PDF、XLSX 或 XLS 文件。")
    except ValueError:
        raise
    except ImportError as exc:
        raise ValueError("服务器缺少此格式的识别组件，请联系开发者。") from exc
    except Exception as exc:
        raise ValueError("课表文件无法读取，请检查文件是否完整、清晰且没有密码。") from exc
    if not preview["slots"]:
        warnings.append("暂未读出课程，可在预览中点格子填写，或换用清晰的完整课表。")
    return {"preview": preview, "warnings": list(dict.fromkeys(warnings)), "method": method}
