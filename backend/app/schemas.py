from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

ShortText = Annotated[
    str,
    StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=300),
]
TextList = Annotated[list[ShortText], Field(max_length=6)]
DiseaseName = Annotated[
    str,
    StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=100),
]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class PossibleCause(StrictModel):
    name: ShortText
    reason: ShortText


class DiseaseCandidate(StrictModel):
    name: DiseaseName
    likelihood: Annotated[
        int,
        Field(
            ge=0,
            le=100,
            description="模型估计的可能性百分比，未经准确率校准，不是确诊概率",
        ),
    ]
    reason: ShortText


class DiagnosisResult(StrictModel):
    status: Literal["suspected_disease", "healthy", "uncertain", "not_tomato"]
    disease_name: DiseaseName | None
    disease_candidates: Annotated[
        list[DiseaseCandidate],
        Field(
            max_length=3,
            description="仅疑似病害时列1至3个候选，首项与disease_name一致，按可能性降序；其他状态为空数组",
        ),
    ]
    summary: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=800)
    ]
    observed_symptoms: TextList
    possible_causes: Annotated[list[PossibleCause], Field(max_length=3)]
    recommendations: TextList
    follow_up_questions: TextList

    @model_validator(mode="after")
    def validate_disease_name(self) -> "DiagnosisResult":
        if (self.status == "suspected_disease") != (self.disease_name is not None):
            raise ValueError("disease_name must be set only for suspected_disease")
        if self.status != "suspected_disease":
            if self.disease_candidates:
                raise ValueError(
                    "disease_candidates must be empty outside suspected_disease"
                )
            return self
        if not self.disease_candidates:
            raise ValueError(
                "suspected_disease requires at least one disease candidate"
            )
        if self.disease_candidates[0].name != self.disease_name:
            raise ValueError("first candidate must match disease_name")
        names = [candidate.name for candidate in self.disease_candidates]
        if len(set(names)) != len(names):
            raise ValueError("disease candidate names must be unique")
        likelihoods = [candidate.likelihood for candidate in self.disease_candidates]
        if likelihoods != sorted(likelihoods, reverse=True):
            raise ValueError(
                "disease candidates must be ordered by descending likelihood"
            )
        return self


class DiagnosisResponse(StrictModel):
    request_id: str
    model: str
    elapsed_ms: int
    result: DiagnosisResult
    notice: str = "本结果仅基于图片进行初步识别，不能替代现场诊断。"
