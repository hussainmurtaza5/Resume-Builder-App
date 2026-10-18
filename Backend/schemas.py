from pydantic import BaseModel, EmailStr, Field

class UserCreate(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=72)


class UserResponse(BaseModel):
    id: int
    email: EmailStr

    class Config:
        from_attributes = True


class Token(BaseModel):
    access_token: str
    token_type: str

class ExperienceBase(BaseModel):
    company: str = Field(min_length=1, max_length=150)
    role: str = Field(min_length=1, max_length=150)
    years: int = Field(ge=0, le=60)


class ExperienceCreate(ExperienceBase):
    pass


class ExperienceResponse(ExperienceBase):
    id: int

    class Config:
        from_attributes = True   # lets Pydantic read from SQLAlchemy objects


class ResumeBase(BaseModel):
    full_name: str = Field(min_length=1, max_length=100)
    email: EmailStr
    phone: str | None = None
    summary: str = Field(default="", max_length=2000)


class ResumeCreate(ResumeBase):
    experiences: list[ExperienceCreate] = []

class ResumeUpdate(BaseModel):
    full_name: str | None = Field(default=None, min_length=1, max_length=100)
    email: EmailStr | None = None
    phone: str | None = None
    summary: str | None = Field(default=None, max_length=2000)
    
class ResumeResponse(ResumeBase):
    id: int
    experiences: list[ExperienceResponse] = []

    class Config:
        from_attributes = True