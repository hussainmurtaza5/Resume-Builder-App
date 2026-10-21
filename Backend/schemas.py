from typing import Any
from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator


# ---------------- Users / Auth ----------------
class UserCreate(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=72)


class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    email: EmailStr


class Token(BaseModel):
    access_token: str
    token_type: str


# ---------------- Shared helpers ----------------
def _clean_bullets(value: list[str]) -> list[str]:
    """Strip bullet strings and drop empty ones."""
    return [b.strip() for b in value if b and b.strip()]


class BulletListMixin(BaseModel):
    bullets: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("bullets", mode="before")
    @classmethod
    def _normalise_bullets(cls, value: Any) -> Any:
        if isinstance(value, list):
            return _clean_bullets(value)
        return value


# ---------------- Experience ----------------
class ExperienceBase(BaseModel):
    company: str = Field(min_length=1, max_length=150)
    role: str = Field(min_length=1, max_length=150)
    years: int = Field(default=0, ge=0, le=60)
    location: str | None = Field(default=None, max_length=120)
    start_date: str | None = Field(
        default=None, max_length=20, description="Free-form date, e.g. 'Jan 2021'"
    )
    end_date: str | None = Field(
        default=None, max_length=20, description="Free-form date, e.g. 'Mar 2023'"
    )
    is_current: bool = False
    description: str = Field(default="", max_length=2000)
    sort_order: int = 0


class ExperienceCreate(ExperienceBase, BulletListMixin):
    pass


class ExperienceUpdate(BaseModel):
    company: str | None = Field(default=None, min_length=1, max_length=150)
    role: str | None = Field(default=None, min_length=1, max_length=150)
    years: int | None = Field(default=None, ge=0, le=60)
    location: str | None = Field(default=None, max_length=120)
    start_date: str | None = Field(default=None, max_length=20)
    end_date: str | None = Field(default=None, max_length=20)
    is_current: bool | None = None
    description: str | None = Field(default=None, max_length=2000)
    bullets: list[str] | None = Field(default=None, max_length=20)
    sort_order: int | None = None

    @field_validator("bullets", mode="before")
    @classmethod
    def _normalise_bullets(cls, value: Any) -> Any:
        if isinstance(value, list):
            return _clean_bullets(value)
        return value


class ExperienceResponse(ExperienceBase, BulletListMixin):
    model_config = ConfigDict(from_attributes=True)

    id: int


# ---------------- Education ----------------
class EducationBase(BaseModel):
    institution: str = Field(min_length=1, max_length=150)
    degree: str = Field(min_length=1, max_length=100)
    field_of_study: str | None = Field(default=None, max_length=120)
    location: str | None = Field(default=None, max_length=120)
    start_date: str | None = Field(
        default=None, max_length=20, description="Free-form date, e.g. 'Aug 2018'"
    )
    end_date: str | None = Field(
        default=None, max_length=20, description="Free-form date, e.g. 'May 2022'"
    )
    is_current: bool = False
    gpa: str | None = Field(default=None, max_length=20, description="e.g. '3.8/4.0'")
    honors_coursework: str = Field(default="", max_length=2000)
    sort_order: int = 0


class EducationCreate(EducationBase):
    pass


class EducationUpdate(BaseModel):
    institution: str | None = Field(default=None, min_length=1, max_length=150)
    degree: str | None = Field(default=None, min_length=1, max_length=100)
    field_of_study: str | None = Field(default=None, max_length=120)
    location: str | None = Field(default=None, max_length=120)
    start_date: str | None = Field(default=None, max_length=20)
    end_date: str | None = Field(default=None, max_length=20)
    is_current: bool | None = None
    gpa: str | None = Field(default=None, max_length=20)
    honors_coursework: str | None = Field(default=None, max_length=2000)
    sort_order: int | None = None


class EducationResponse(EducationBase):
    model_config = ConfigDict(from_attributes=True)

    id: int


# ---------------- Skills ----------------
class SkillBase(BaseModel):
    category: str = Field(min_length=1, max_length=60)
    name: str = Field(min_length=1, max_length=60)
    sort_order: int = 0


class SkillCreate(BaseModel):
    """Accepts a single skill (`name`) and/or a batch (`names`) under one category."""

    category: str = Field(min_length=1, max_length=60)
    name: str | None = Field(default=None, min_length=1, max_length=60)
    names: list[str] = Field(default_factory=list, max_length=50)
    sort_order: int = 0

    @field_validator("names", mode="before")
    @classmethod
    def _clean_names(cls, value: Any) -> Any:
        if isinstance(value, list):
            return [n.strip() for n in value if n and n.strip()]
        return value


class SkillUpdate(BaseModel):
    category: str | None = Field(default=None, min_length=1, max_length=60)
    name: str | None = Field(default=None, min_length=1, max_length=60)
    sort_order: int | None = None


class SkillResponse(SkillBase):
    model_config = ConfigDict(from_attributes=True)

    id: int


class SkillCategoryResponse(BaseModel):
    """Skills grouped by category for the 'categorised skills' section."""

    category: str
    skills: list[SkillResponse] = Field(default_factory=list)


# ---------------- Projects ----------------
class ProjectBase(BaseModel):
    title: str = Field(min_length=1, max_length=150)
    tech_stack: list[str] = Field(default_factory=list, max_length=30)
    description: str = Field(default="", max_length=3000)
    github_url: str | None = Field(default=None, max_length=255)
    live_url: str | None = Field(default=None, max_length=255)
    sort_order: int = 0


class ProjectCreate(ProjectBase, BulletListMixin):
    pass


class ProjectUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=150)
    tech_stack: list[str] | None = Field(default=None, max_length=30)
    description: str | None = Field(default=None, max_length=3000)
    bullets: list[str] | None = Field(default=None, max_length=20)
    github_url: str | None = Field(default=None, max_length=255)
    live_url: str | None = Field(default=None, max_length=255)
    sort_order: int | None = None

    @field_validator("bullets", mode="before")
    @classmethod
    def _normalise_bullets(cls, value: Any) -> Any:
        if isinstance(value, list):
            return _clean_bullets(value)
        return value


class ProjectResponse(ProjectBase, BulletListMixin):
    model_config = ConfigDict(from_attributes=True)

    id: int


# ---------------- Resume ----------------
class ResumeBase(BaseModel):
    title: str | None = Field(default=None, max_length=150)
    full_name: str = Field(min_length=1, max_length=100)
    email: EmailStr
    phone: str | None = Field(default=None, max_length=30)
    location: str | None = Field(default=None, max_length=120)
    linkedin_url: str | None = Field(default=None, max_length=255)
    github_url: str | None = Field(default=None, max_length=255)
    portfolio_url: str | None = Field(default=None, max_length=255)
    summary: str = Field(default="", max_length=2000)


class ResumeCreate(ResumeBase):
    experiences: list[ExperienceCreate] = Field(default_factory=list)
    educations: list[EducationCreate] = Field(default_factory=list)
    skills: list[SkillCreate] = Field(default_factory=list)
    projects: list[ProjectCreate] = Field(default_factory=list)


class ResumeUpdate(BaseModel):
    title: str | None = Field(default=None, max_length=150)
    full_name: str | None = Field(default=None, min_length=1, max_length=100)
    email: EmailStr | None = None
    phone: str | None = Field(default=None, max_length=30)
    location: str | None = Field(default=None, max_length=120)
    linkedin_url: str | None = Field(default=None, max_length=255)
    github_url: str | None = Field(default=None, max_length=255)
    portfolio_url: str | None = Field(default=None, max_length=255)
    summary: str | None = Field(default=None, max_length=2000)


class ResumeDuplicateRequest(BaseModel):
    """Optional overrides applied while cloning a resume."""

    title: str | None = Field(default=None, max_length=150)
    full_name: str | None = Field(default=None, min_length=1, max_length=100)
    summary: str | None = Field(default=None, max_length=2000)


class ResumeResponse(ResumeBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    experiences: list[ExperienceResponse] = Field(default_factory=list)
    educations: list[EducationResponse] = Field(default_factory=list)
    skills: list[SkillResponse] = Field(default_factory=list)
    projects: list[ProjectResponse] = Field(default_factory=list)


# ---------------- AI ----------------
class AIGenerateRequest(BaseModel):
    full_name: str
    experiences: list[dict] = Field(default_factory=list)
    current_summary: str = ""


class AIGenerateResponse(BaseModel):
    summary: str


class AIRewriteBulletRequest(BaseModel):
    bullet: str = Field(min_length=3, max_length=1000)
    role: str | None = Field(default=None, max_length=150)
    company: str | None = Field(default=None, max_length=150)
    target_role: str | None = Field(default=None, max_length=150)
    tone: str | None = Field(default=None, max_length=50)


class AIRewriteBulletResponse(BaseModel):
    variants: list[str] = Field(default_factory=list)
    formula: str = "Accomplished [X], as measured by [Y], by doing [Z]"
