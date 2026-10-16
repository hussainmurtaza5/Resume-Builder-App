from fastapi import FastAPI
from pydantic import BaseModel, EmailStr, Field

app = FastAPI(title="Resume Builder API")

class ExperienceItem(BaseModel):
    company: str = Field(min_length=1, max_length=150)
    role: str = Field(min_length=1, max_length=150)
    years: int = Field(ge=0, le=60)

class ResumeCreate(BaseModel):
    full_name: str = Field(min_length=1, max_length=100)
    email: EmailStr
    phone: str | None = None
    summary: str = Field(default="", max_length=2000)
    experiences: list[ExperienceItem] = []

class ResumeResponse(ResumeCreate):
    id: int
    
    

@app.get("/")
def read_root():
    return {"message": "Backend is alive"}


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/hello/{name}")
def hello(name: str):
    return {"message": f"Hello {name}"}

@app.post("/resumes", response_model=ResumeResponse, status_code=201)
def create_resume(resume: ResumeCreate):
    return {"id": 1, **resume.model_dump()}