"""Fixed crop context; user input never supplies model instructions."""

from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal

CropId = Literal[
    "tomato", "cucumber", "pepper", "eggplant", "potato", "chinese_cabbage"
]


@dataclass(frozen=True)
class Crop:
    name: str
    aliases: tuple[str, ...]
    photo_tip: str
    prompt_hint: str


CROPS = MappingProxyType(
    {
        "tomato": Crop(
            "番茄",
            ("番茄", "西红柿"),
            "拍清叶片正反面，并补拍整株和果实。",
            "结合整株、叶片及果实可见特征核实是否为番茄，不凭用户选择认定。",
        ),
        "cucumber": Crop(
            "黄瓜",
            ("黄瓜",),
            "拍清叶片正反面，并补拍藤蔓和果实。",
            "结合藤蔓、叶片及果实可见特征核实是否为黄瓜，证据不足时保留不确定性。",
        ),
        "pepper": Crop(
            "辣椒",
            ("辣椒", "甜椒"),
            "拍清叶片正反面，并补拍整株和椒果。",
            "结合整株、叶片及椒果可见特征核实是否为辣椒，不能仅凭叶片斑点认定作物。",
        ),
        "eggplant": Crop(
            "茄子",
            ("茄子",),
            "拍清叶片正反面，并补拍整株、花或果实。",
            "结合整株、叶片、花及果实可见特征核实是否为茄子，仅陈述可见证据。",
        ),
        "potato": Crop(
            "马铃薯",
            ("马铃薯", "土豆"),
            "拍清叶片正反面，并补拍整株和受影响部位。",
            "结合整株及叶片形态核实是否为马铃薯，不推断图片外的块茎或症状。",
        ),
        "chinese_cabbage": Crop(
            "大白菜",
            ("大白菜", "结球白菜"),
            "拍清叶片正反面，并补拍整株及叶球。",
            "范围仅限大白菜，不包括小白菜和甘蓝；结合整株及叶球形态核实，无法区分时明确未核实。",
        ),
    }
)
