from __future__ import annotations

from sqlalchemy.orm import Session

from .models import User
from .preferences_models import UserSettings


PREFERENCE_KEYS = ("headingDensity", "enthusiasm", "explanationBreadth", "paragraphDensity", "paragraphLength")
LEVELS = ("low", "medium", "high")
GREETING_CATEGORIES = ("math", "life", "novel", "drama", "film", "game", "absurd")
DEFAULT_PREFERENCES = {key: "medium" for key in PREFERENCE_KEYS}

PREFERENCE_TEXT = {
    "headingDensity": ("只在主题切换时设标题，避免细碎小标题。", "按主要知识点设标题，保持两至三级结构。", "为各个知识点和必要推导设置清晰标题。"),
    "enthusiasm": ("语气平实、克制，直接讲内容。", "语气自然、有耐心，用必要的过渡帮助理解。", "语气活泼、有参与感，可用少量贴切类比，避免夸张和空泛鼓励。"),
    "explanationBreadth": ("围绕课堂原有内容，补全理解所需的定义和条件。", "解释关键因果，补充少量直接相关的例子和联系。", "适当展开推导、背景与跨知识点联系；拓展内容标明来源于解释，不能冒充老师原话。"),
    "paragraphDensity": ("同一意思尽量连贯叙述，减少分段。", "按意思和推理步骤自然分段。", "在话题或推理步骤变化时及时分段，便于逐段阅读。"),
    "paragraphLength": ("每段以一至三句为主，公式和必要条件保持完整。", "每段以三至五句为主，按内容调整。", "可用较长段落完整讲清一个问题，避免超长文字墙。"),
}


def settings_json(db: Session, user: User) -> dict:
    row = db.get(UserSettings, user.id)
    return {
        "preferences": {**DEFAULT_PREFERENCES, **(row.preferences or {})} if row else dict(DEFAULT_PREFERENCES),
        "customPrompt": row.custom_prompt if row else user.default_prompt or "",
        "greetingCategories": list(row.greeting_categories or []) if row else [],
    }


def save_settings(db: Session, user: User, changes: dict) -> dict:
    row = db.get(UserSettings, user.id)
    if row is None:
        row = UserSettings(user_id=user.id, preferences=dict(DEFAULT_PREFERENCES),
                           custom_prompt=user.default_prompt or "", greeting_categories=[])
        db.add(row)
    if "preferences" in changes:
        options = changes["preferences"]
        if not isinstance(options, dict) or any(key not in PREFERENCE_KEYS for key in options):
            raise ValueError("整理偏好选项不正确。")
        if any(value not in LEVELS for value in options.values()):
            raise ValueError("整理偏好请选择低、中、高三档。")
        row.preferences = {**DEFAULT_PREFERENCES, **(row.preferences or {}), **options}
    if "customPrompt" in changes:
        custom = changes["customPrompt"]
        if not isinstance(custom, str) or len(custom) > 12000:
            raise ValueError("自定义整理要求最多 12000 个字符。")
        row.custom_prompt = custom.strip()
    if "greetingCategories" in changes:
        categories = changes["greetingCategories"]
        if not isinstance(categories, list) or len(categories) > len(GREETING_CATEGORIES) or any(
            category not in GREETING_CATEGORIES for category in categories
        ):
            raise ValueError("欢迎词类别不正确。")
        row.greeting_categories = list(dict.fromkeys(categories))
    db.flush()
    return settings_json(db, user)


def apply_preferences(db: Session, user: User, base_prompt: str, *, override: str = "") -> str:
    row = db.get(UserSettings, user.id)
    if row is None and user.default_prompt.strip():
        return override.strip() or base_prompt
    options = {**DEFAULT_PREFERENCES, **(row.preferences or {})} if row else dict(DEFAULT_PREFERENCES)
    # Medium means the shared default, not another competing prose template.
    lines = [PREFERENCE_TEXT[key][LEVELS.index(options[key])] for key in PREFERENCE_KEYS if options[key] != "medium"]
    parts = [base_prompt]
    if lines:
        parts.append("整理偏好：\n" + "\n".join(lines))
    if row and row.custom_prompt.strip():
        from .logic import current_builtin_prompt
        custom = current_builtin_prompt(row.custom_prompt.strip())
        if custom != base_prompt:
            parts.append("用户自定义要求（与上面的整理偏好或风格要求冲突时，以本段为准）：\n" + custom)
    if override.strip():
        parts.append("本次整理的补充要求：\n" + override.strip())
    return "\n\n".join(parts)
