"""Bounded Commons image discovery for a completed note, with durable credits.

Only short concept names leave the application. The worker never fetches a URL
supplied by a user/model: API and media hosts are fixed and redirects disabled.
"""
from __future__ import annotations

import html
import json
import re
import time
from urllib.parse import unquote, urlsplit

import httpx


API_URL = "https://commons.wikimedia.org/w/api.php"
USER_AGENT = "Tingji/3.1 (educational note illustrations; https://tingji-henry.vercel.app)"
IMAGE_METADATA = re.compile(r"(?ms)^```note-images[ \t]*\r?\n(.*?)\r?\n```[ \t]*\r?(?:\n|$)")
_CODE = re.compile(r"(?ms)^ {0,3}(`{3,}|~{3,})[^\n]*\n.*?^ {0,3}\1[ \t]*(?:\n|$)")
_HEADINGS = re.compile(r"(?m)^ {0,3}(#{1,4})[ \t]+([^\n]+)")
_EXTRA = re.compile(r"^(?:额外探索|继续探索|拓展探索|课程信息|来源定位|参考资料|参考文献)")
_RASTER = re.compile(r"\.(?:png|jpe?g|webp)$", re.I)
_MIME = {"image/png", "image/jpeg", "image/webp"}
_STOP = {"the", "of", "and", "for", "in", "to", "a", "an", "with", "diagram"}
MAX_IMAGES = 8
MAX_QUERIES = 12
SEARCH_SECONDS = 30
# Translation hints identify concepts, never select pre-chosen images.
_ALIASES = {
    "Transformer": "transformer architecture", "Transformer encoder": "transformer encoder", "Transformer decoder": "transformer decoder",
    "RNN": "recurrent neural network", "LSTM": "long short-term memory",
    "循环神经网络": "recurrent neural network", "递归神经网络": "recursive neural network",
    "长短期记忆": "long short-term memory", "长短时记忆": "long short-term memory", "N-gram": "n-gram language model",
    "图神经网络": "graph neural network", "图卷积神经网络": "graph convolutional network",
    "自注意力": "self attention", "注意力机制": "attention mechanism", "神经网络": "neural network",
    "卷积神经网络": "convolutional neural network", "反向传播": "backpropagation", "梯度下降": "gradient descent",
    "词嵌入": "word embedding", "分词": "tokenization", "傅里叶变换": "Fourier transform",
    "拉普拉斯变换": "Laplace transform", "PID": "PID controller", "比例积分微分": "PID controller",
    "状态空间": "state space control", "卡尔曼滤波": "Kalman filter", "奈奎斯特": "Nyquist plot",
    "伯德图": "Bode plot", "波特图": "Bode plot", "直流电机": "DC motor", "步进电机": "stepper motor",
    "砂型铸造": "sand casting", "压铸": "die casting", "熔模铸造": "investment casting",
    "铸造": "metal casting", "车削": "turning machining", "铣削": "milling machining",
    "逆运动学": "inverse kinematics", "雅可比": "Jacobian robotics", "齿轮": "gear",
    "激光雷达": "lidar", "同步定位与地图构建": "simultaneous localization and mapping",
    "轧制": "metal rolling", "锻造": "forging", "挤压": "metal extrusion", "焊接": "welding",
    "铸型": "casting mold", "冒口": "casting riser", "缩孔": "casting shrinkage",
    "相图": "phase diagram", "晶体结构": "crystal structure", "热处理": "heat treatment",
    "机械臂": "robotic arm", "四杆机构": "four bar linkage", "曲柄滑块": "slider crank",
    "湘江战役": "Battle of Xiangjiang", "遵义会议": "Zunyi Conference", "长征": "Long March",
    "井冈山": "Jinggangshan", "辛亥革命": "Xinhai Revolution", "五四运动": "May Fourth Movement",
    "传染链": "chain of infection", "流行曲线": "epidemic curve", "霍乱": "cholera",
    "流感病毒": "influenza virus", "冠状病毒": "coronavirus", "疫苗": "vaccination",
}


def _text(value: object) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]*>", " ", str(value or "")))).strip()


def safe_image_url(value: object) -> bool:
    if not isinstance(value, str) or len(value) > 1800 or re.search(r"[\s\\\x00-\x1f]", value):
        return False
    try:
        url = urlsplit(value)
        path = unquote(url.path)
        return bool(url.scheme == "https" and url.hostname in {"upload.wikimedia.org", "thumb.wikimedia.org"}
                    and not url.username and not url.password and url.port is None and not url.fragment
                    and path.startswith("/wikipedia/commons/") and _RASTER.search(path)
                    and not any(part in {".", ".."} for part in path.split("/"))
                    and not re.search(r"[\\\x00-\x1f]", path))
    except ValueError:
        return False


def _safe_link(value: object, *, license: bool = False) -> bool:
    if not isinstance(value, str) or len(value) > 1800 or re.search(r"[\s\\\x00-\x1f]", value):
        return False
    try:
        url = urlsplit(value)
        hosts = {"commons.wikimedia.org", "creativecommons.org"} if license else {"commons.wikimedia.org"}
        return bool(url.scheme == "https" and url.hostname in hosts and not url.username and not url.password
                    and url.port is None and (license or url.path.startswith("/wiki/File:")))
    except ValueError:
        return False


def validate_image_resource(entry: object) -> dict | None:
    """Validate persisted metadata again before using it; no global registry."""
    if not isinstance(entry, dict) or not safe_image_url(entry.get("imagePath")):
        return None
    if not _safe_link(entry.get("url")) or not _safe_link(entry.get("licenseUrl"), license=True):
        return None
    result = {"imagePath": entry["imagePath"], "url": entry["url"], "licenseUrl": entry["licenseUrl"]}
    for key, maximum in (("title", 240), ("author", 600), ("license", 160), ("credit", 1200), ("query", 100)):
        value = entry.get(key, "")
        if not isinstance(value, str) or len(value) > maximum:
            return None
        value = _text(value)
        if key in {"title", "author", "license"} and not value:
            return None
        if value:
            result[key] = value
    for key in ("width", "height"):
        value = entry.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and 0 < value <= 100000:
            result[key] = value
    return result


def note_image_resources(summary: str) -> list[dict]:
    result, seen = [], set()
    for match in IMAGE_METADATA.finditer(summary):
        if len(match[1]) > 32000:
            continue
        try:
            entries = json.loads(match[1])
        except (ValueError, TypeError):
            continue
        if not isinstance(entries, list):
            continue
        for entry in entries[:8]:
            resource = validate_image_resource(entry)
            if resource and resource["imagePath"] not in seen:
                result.append(resource)
                seen.add(resource["imagePath"])
    return result[:8]


def with_image_resources(summary: str, entries: list[dict]) -> str:
    body = IMAGE_METADATA.sub("", summary).rstrip()
    valid = [resource for entry in entries if (resource := validate_image_resource(entry))][:8]
    return body + ("\n\n```note-images\n" + json.dumps(valid, ensure_ascii=False, separators=(",", ":"))
                   + "\n```" if valid else "")


def _query(label: str) -> str:
    label = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", label)
    label = re.sub(r"^[\d.、\s]+", "", label).strip(" #*_`：:？?")
    if re.search(r"https?://|@|[/\\]", label):
        return ""
    bilingual = re.search(r"[（(]([A-Za-z][A-Za-z0-9 \-]{3,70})[)）]", label)
    if bilingual:
        label = bilingual[1].strip()
    # Match longest terms first so convolutional networks do not become a
    # generic neural-network illustration.
    for term in sorted(_ALIASES, key=len, reverse=True):
        # Chinese compound nouns must not silently lose their prefix: for
        # example, an unknown X神经网络 must never fall back to 神经网络.
        if re.search(r"(?<![a-z0-9])" + re.escape(term) + r"(?![a-z0-9])", label, re.I) if term.isascii() else re.search(r"(?:^|[\s：:、与和])" + re.escape(term) + r"(?=$|[\s（(：:、的与和])", label):
            return _ALIASES[term]
    label = re.sub(r"^(?:什么是|如何理解|认识|理解)", "", label)
    label = re.split(r"[：:——]|的(?:区别|比较|优劣|应用|原理|结构|过程|条件|关系)", label)[0].strip()
    # Short labels only; never send paragraphs, links, email or private paths.
    if not 3 <= len(label) <= 70 or re.search(r"https?://|@|[/\\]|[。！？!?；;]", label):
        return ""
    if label in {"课堂笔记", "课堂", "内容", "总结", "基本概念", "核心概念", "关键概念", "对比", "应用场景", "知识关系"}:
        return ""
    return label


def _sections(summary: str) -> list[tuple[str, str, int, int]]:
    visible = _CODE.sub(lambda match: re.sub(r"[^\n]", " ", match[0]), summary)
    headings = list(_HEADINGS.finditer(visible))
    stop = next((match.start() for match in headings if _EXTRA.match(match[2])), len(summary))
    headings = [match for match in headings if match.start() < stop]
    concepts = []
    for block in re.finditer(r"(?ms)^```study\s*\n(.*?)\n```", summary):
        try:
            study = json.loads(block[1])
            for concept in study.get("concepts", []) if isinstance(study, dict) else []:
                if isinstance(concept, dict) and isinstance(concept.get("term"), str):
                    term = concept["term"]
                    if len(term) <= 100:
                        concepts.append(term)
        except (TypeError, ValueError):
            continue
    results = []
    for index, match in enumerate(headings):
        end = headings[index + 1].start() if index + 1 < len(headings) else stop
        body = visible[match.end():end]
        if len(_text(body)) < 35 or re.search(r"!\[[^\n]*\]\(", body):
            continue
        label = match[2]
        # Prefer actual study concept names present in this section, including
        # bilingual terminology, over broad prose headings.
        terms = [term for term in sorted(concepts, key=len, reverse=True) if term in label]
        # Headings are often a question or an argument. Also search concrete
        # study concepts in the section, without sending any body paragraphs.
        terms += [term for term in concepts if term in body and term not in terms]
        aliases = []
        for term in sorted(_ALIASES, key=len, reverse=True):
            # Do not recover a generic noun from an unknown compound such as
            # 新型未知神经网络; explicit study terms remain the preferred source.
            pattern = r"(?<![a-z0-9\u3400-\u9fff])" + re.escape(term) + r"(?![a-z0-9])"
            if re.search(pattern, label + "\n" + body, re.I) and not any(term in longer for longer in aliases):
                aliases.append(term)
        terms += [term for term in aliases if term not in terms]
        choices = [(term, _query(term)) for term in terms]
        choices.append((label, _query(label)))
        seen = set()
        for term, query in choices:
            if query and query.casefold() not in seen:
                seen.add(query.casefold())
                results.append((term, query, match.end(), end))
                if len(seen) >= 3:
                    break
    # Try one concrete subject in every section before using alternatives in
    # earlier sections, so a long note gains illustrations throughout.
    ranks = {}
    def priority(section):
        key = section[3]
        rank = ranks.get(key, 0)
        ranks[key] = rank + 1
        return rank
    return sorted(results, key=priority)


def _terms(query: str) -> list[str]:
    words = re.findall(r"[a-z][a-z0-9-]*|[\u3400-\u9fff]{2,}", query.casefold())
    return [word for word in words if word not in _STOP]


def _candidate(page: dict, query: str) -> tuple[float, dict] | None:
    infos = page.get("imageinfo", [])
    info = next(iter(infos), {}) if isinstance(infos, list) else {}
    if not isinstance(info, dict):
        return None
    metadata = info.get("extmetadata", {})
    if not isinstance(metadata, dict):
        return None
    meta = lambda key: _text(metadata.get(key, {}).get("value", "")) if isinstance(metadata.get(key, {}), dict) else ""
    if meta("NonFree").casefold() in {"true", "1"} or meta("Restrictions") or meta("DeletionReason"):
        return None
    title = _text(page.get("title", "")).removeprefix("File:")
    description = meta("ImageDescription")
    words = _terms(query)
    evidence = (title + " " + description + " " + meta("Categories")).casefold().replace("_", " ")
    matches = [word for word in words if (re.search(r"(?<![a-z0-9])" + re.escape(word) + r"s?(?![a-z0-9])", evidence)
                                         if word.isascii() else word in evidence)]
    if not words or len(matches) < min(3, len(words)):
        return None
    # A lone generic noun is weak evidence when the identifying term is absent.
    if words[0] not in matches:
        return None
    # A specialized architecture is not a diagram of its parent concept.
    # Its distinguishing qualifier must also be present in the note's query.
    for qualifier in ("bidirectional", "genomic", "protein", "quantum", "convolutional", "recursive", "graph"):
        if re.search(r"\b" + qualifier + r"\b", title, re.I) and qualifier not in query.casefold():
            return None
    path = info.get("thumburl") or info.get("url")
    mime = info.get("thumbmime") if info.get("thumburl") else info.get("mime")
    if mime and mime not in _MIME:
        return None
    width, height = info.get("thumbwidth", info.get("width", 0)), info.get("thumbheight", info.get("height", 0))
    if not isinstance(width, int) or not isinstance(height, int) or width < 250 or height < 140 or not .5 < width / height < 5:
        return None
    license_url = meta("LicenseUrl").replace("http://", "https://", 1)
    source = info.get("descriptionurl", "")
    license_name = meta("LicenseShortName")
    # Public-domain status is described on the returned file page when Commons
    # does not supply a separate licence URL.
    if not license_url and license_name.casefold() == "public domain":
        license_url = source
    entry = {"title": title, "imagePath": path, "url": source,
             "author": meta("Attribution") or meta("Artist"), "license": license_name,
             "licenseUrl": license_url, "width": width, "height": height, "query": query}
    credit = meta("Credit")
    if credit and credit.casefold() not in {"own work", "self-made", "自己的作品"} and not credit.startswith("Transferred from"):
        entry["credit"] = credit
    entry = validate_image_resource(entry)
    if not entry:
        return None
    title_matches = sum(word in title.casefold().replace("_", " ") for word in words)
    extra_title_words = max(0, len(_terms(re.sub(r"\.[a-z]+$", "", title, flags=re.I))) - title_matches)
    diagram = bool(re.search(r"diagram|schematic|block|\.svg$", title, re.I))
    foreign = bool(re.search(r"(?:^|[ _.-])(?:de|es|fr|ru|it|pt|pl|ja|ko|uk|sv|fa|bn|ar|he)(?:[ _.-]|$)", title, re.I))
    if foreign:
        return None
    preferred_language = bool(re.search(r"(?:^|[ _.-])(?:en|zh|cn)(?:[ _.-]|$)", title, re.I))
    return len(matches) * 2 + title_matches + diagram + preferred_language - .4 * extra_title_words, entry


def search_commons_image(query: str, *, client: httpx.Client, deadline: float) -> dict | None:
    """One search and at most two image checks; no retry on network failure."""
    if deadline <= time.monotonic():
        return None
    params = {"action": "query", "format": "json", "formatversion": 2, "generator": "search",
              "gsrsearch": query.replace('"', ""), "gsrnamespace": 6, "gsrlimit": 8,
              "prop": "imageinfo", "iiprop": "url|size|mime|thumbmime|extmetadata", "iiurlwidth": 960,
              "iiextmetadatafilter": "Artist|Attribution|Credit|LicenseShortName|LicenseUrl|ImageDescription|Categories|Restrictions|NonFree|DeletionReason",
              "maxlag": 5}
    response = client.get(API_URL, params=params, timeout=min(4, max(.2, deadline - time.monotonic())))
    response.raise_for_status()
    if len(response.content) > 400000:
        return None
    data = response.json()
    if not isinstance(data, dict) or not isinstance(data.get("query", {}), dict):
        return None
    pages = data.get("query", {}).get("pages", [])
    if not isinstance(pages, list):
        return None
    candidates = [candidate for page in pages if isinstance(page, dict) and (candidate := _candidate(page, query))]
    for _, resource in sorted(candidates, key=lambda pair: -pair[0])[:2]:
        if deadline <= time.monotonic():
            return None
        check = client.head(resource["imagePath"], timeout=min(3, max(.2, deadline - time.monotonic())))
        if check.status_code == 200 and check.headers.get("content-type", "").split(";")[0].lower() in _MIME:
            return resource
    return None


def _figure(resource: dict, description: str = "") -> str:
    label = lambda value: value.replace("[", "［").replace("]", "］").replace("\n", " ")
    title = label(re.sub(r"^[\d.、\s]+", "", description).strip(" #*_`")) or label(resource["title"])
    return (f"![{title}]({resource['imagePath']})\n\n"
            f"来源：[{label(resource['author'])}]({resource['url']})；"
            f"[{label(resource['license'])}]({resource['licenseUrl']})。")


def enrich_note_images(summary: str, *, limit: int = MAX_IMAGES, client: httpx.Client | None = None) -> str:
    """Place relevant images beside their explanation and retain source metadata.

    An unchanged note is the normal result of no match, timeout or unavailable
    Commons. This must never turn a completed paid generation into a failed job.
    """
    existing = note_image_resources(summary)
    if existing or limit <= 0:
        return summary
    candidates = _sections(summary)
    if not candidates:
        return summary
    own_client = client is None
    client = client or httpx.Client(headers={"User-Agent": USER_AGENT}, follow_redirects=False)
    deadline = time.monotonic() + SEARCH_SECONDS
    insertions, resources, seen_queries, seen_urls, illustrated = [], [], set(), set(), set()
    try:
        for label, query, start, end in candidates:
            if len(seen_queries) >= MAX_QUERIES or len(resources) >= min(MAX_IMAGES, limit) or time.monotonic() >= deadline:
                break
            if end in illustrated or query.casefold() in seen_queries:
                continue
            seen_queries.add(query.casefold())
            try:
                resource = search_commons_image(query, client=client, deadline=deadline)
            except (httpx.HTTPError, ValueError, TypeError, KeyError):
                continue
            if resource and resource["imagePath"] not in seen_urls:
                # End the paragraph that introduces the subject; never split
                # a formula, fenced diagram, table, or list to insert a photo.
                body = summary[start:end]
                visible_body = _CODE.sub(lambda match: re.sub(r"[^\n]", " ", match[0]), body)
                occurrence = visible_body.find(label)
                paragraph_end = re.search(r"\n[ \t]*\n", visible_body[max(0, occurrence):]) if occurrence >= 0 else None
                position = start + occurrence + paragraph_end.end() if paragraph_end else end
                insertions.append((position, _figure(resource, label)))
                resources.append(resource)
                seen_urls.add(resource["imagePath"])
                illustrated.add(end)
    finally:
        if own_client:
            client.close()
    for position, figure in sorted(insertions, reverse=True):
        summary = summary[:position].rstrip() + "\n\n" + figure + "\n\n" + summary[position:].lstrip()
    return with_image_resources(summary, resources) if resources else summary
