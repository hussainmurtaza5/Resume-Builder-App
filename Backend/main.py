from fastapi import Body, Depends, FastAPI, HTTPException
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload
from jose import JWTError, jwt
from dotenv import load_dotenv
from groq import Groq
import json
import os
import re

load_dotenv()

import models
import schemas
from database import engine, SessionLocal
from auth import hash_password, verify_password, create_access_token, SECRET_KEY, ALGORITHM
from fastapi.middleware.cors import CORSMiddleware
from migrations import run_migrations

models.Base.metadata.create_all(bind=engine)
run_migrations(engine)

app = FastAPI(title="Resume Builder API")

groq_api_key = os.getenv("GROQ_API_KEY")
groq_client = Groq(api_key=groq_api_key) if groq_api_key else None

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        origin.strip()
        for origin in os.getenv(
            "FRONTEND_ORIGINS",
            "http://localhost:5173,http://127.0.0.1:5173",
        ).split(",")
        if origin.strip()
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/login")

XYZ_FORMULA = "Accomplished [X], as measured by [Y], by doing [Z]"


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_current_user(
    token: str = Depends(oauth2_scheme),
    db: Session = Depends(get_db)
) -> models.User:
    credentials_exception = HTTPException(
        status_code=401,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id: str = payload.get("sub")
        if user_id is None:
            raise credentials_exception
        user_id_int = int(user_id)
    except (JWTError, ValueError):
        raise credentials_exception

    user = db.query(models.User).filter(models.User.id == user_id_int).first()
    if user is None:
        raise credentials_exception

    return user


# ------------------------------------------------------------------
# Shared helpers
# ------------------------------------------------------------------
def _owned_resume(db: Session, resume_id: int, user: models.User) -> models.Resume:
    """Return the resume only if it belongs to the given user."""
    resume = db.query(models.Resume).filter(
        models.Resume.id == resume_id,
        models.Resume.user_id == user.id,
    ).first()
    if resume is None:
        raise HTTPException(status_code=404, detail="Resume not found")
    return resume


def _full_resume(db: Session, resume_id: int, user: models.User) -> models.Resume:
    """Return the resume with every section eagerly loaded."""
    resume = db.query(models.Resume).options(
        selectinload(models.Resume.experiences),
        selectinload(models.Resume.educations),
        selectinload(models.Resume.skills),
        selectinload(models.Resume.projects),
    ).filter(
        models.Resume.id == resume_id,
        models.Resume.user_id == user.id,
    ).first()
    if resume is None:
        raise HTTPException(status_code=404, detail="Resume not found")
    return resume


def _child_or_404(db: Session, model, child_id: int, resume_id: int):
    """Return a section row scoped to the resume (prevents cross-resume access)."""
    child = db.query(model).filter(
        model.id == child_id,
        model.resume_id == resume_id,
    ).first()
    if child is None:
        raise HTTPException(status_code=404, detail=f"{model.__name__} not found")
    return child


def _apply_update(instance, payload, model_cls):
    """Copy the provided fields onto a model instance.

    Explicit `null` values are ignored for NOT NULL columns so a partial
    update can never break the row.
    """
    for field, value in payload.model_dump(exclude_unset=True).items():
        column = model_cls.__table__.columns.get(field)
        if column is not None and not column.nullable and value is None:
            continue
        setattr(instance, field, value)


def _experience_kwargs(payload: schemas.ExperienceBase) -> dict:
    return {
        "company": payload.company,
        "role": payload.role,
        "years": payload.years,
        "location": payload.location,
        "start_date": payload.start_date,
        "end_date": payload.end_date,
        "is_current": payload.is_current,
        "description": payload.description,
        "bullets": list(payload.bullets),
        "sort_order": payload.sort_order,
    }


def _education_kwargs(payload: schemas.EducationBase) -> dict:
    return {
        "institution": payload.institution,
        "degree": payload.degree,
        "field_of_study": payload.field_of_study,
        "location": payload.location,
        "start_date": payload.start_date,
        "end_date": payload.end_date,
        "is_current": payload.is_current,
        "gpa": payload.gpa,
        "honors_coursework": payload.honors_coursework,
        "sort_order": payload.sort_order,
    }


def _project_kwargs(payload: schemas.ProjectBase) -> dict:
    return {
        "title": payload.title,
        "tech_stack": list(payload.tech_stack),
        "description": payload.description,
        "bullets": list(payload.bullets),
        "github_url": payload.github_url,
        "live_url": payload.live_url,
        "sort_order": payload.sort_order,
    }


# ------------------------------------------------------------------
# Health
# ------------------------------------------------------------------
@app.get("/")
def read_root():
    return {"message": "Backend is alive"}


@app.get("/health")
def health():
    return {"status": "ok"}


# ------------------------------------------------------------------
# Auth
# ------------------------------------------------------------------
@app.post("/register", response_model=schemas.UserResponse, status_code=201)
def register(payload: schemas.UserCreate, db: Session = Depends(get_db)):
    clean_email = payload.email.lower().strip()
    existing = db.query(models.User).filter(func.lower(models.User.email) == clean_email).first()
    if existing is not None:
        raise HTTPException(status_code=400, detail="Email already registered")

    user = models.User(
        email=clean_email,
        hashed_password=hash_password(payload.password),
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@app.post("/login", response_model=schemas.Token)
def login(
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: Session = Depends(get_db),
):
    clean_username = form_data.username.lower().strip()
    user = db.query(models.User).filter(func.lower(models.User.email) == clean_username).first()

    if user is None or not verify_password(form_data.password, user.hashed_password):
        raise HTTPException(
            status_code=401,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    access_token = create_access_token({"sub": str(user.id)})
    return {"access_token": access_token, "token_type": "bearer"}


# ------------------------------------------------------------------
# Resumes
# ------------------------------------------------------------------
@app.post("/resumes", response_model=schemas.ResumeResponse, status_code=201)
def create_resume(
    payload: schemas.ResumeCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    resume = models.Resume(
        title=payload.title,
        full_name=payload.full_name,
        email=payload.email,
        phone=payload.phone,
        location=payload.location,
        linkedin_url=payload.linkedin_url,
        github_url=payload.github_url,
        portfolio_url=payload.portfolio_url,
        summary=payload.summary,
        user_id=current_user.id,
    )
    resume.experiences = [
        models.Experience(**_experience_kwargs(exp)) for exp in payload.experiences
    ]
    resume.educations = [
        models.Education(**_education_kwargs(edu)) for edu in payload.educations
    ]
    resume.skills = [
        models.Skill(category=skill.category, name=skill.name, sort_order=skill.sort_order)
        for skill in payload.skills
    ]
    resume.projects = [
        models.Project(**_project_kwargs(project)) for project in payload.projects
    ]

    db.add(resume)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=400, detail="Duplicate skill in resume")

    return _full_resume(db, resume.id, current_user)


@app.get("/resumes", response_model=list[schemas.ResumeResponse])
def list_resumes(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    resumes = db.query(models.Resume).options(
        selectinload(models.Resume.experiences),
        selectinload(models.Resume.educations),
        selectinload(models.Resume.skills),
        selectinload(models.Resume.projects),
    ).filter(models.Resume.user_id == current_user.id).all()
    return resumes


@app.get("/resumes/{resume_id}", response_model=schemas.ResumeResponse)
def get_resume(
    resume_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    return _full_resume(db, resume_id, current_user)


@app.patch("/resumes/{resume_id}", response_model=schemas.ResumeResponse)
def update_resume(
    resume_id: int,
    payload: schemas.ResumeUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    resume = _owned_resume(db, resume_id, current_user)
    _apply_update(resume, payload, models.Resume)
    db.commit()
    return _full_resume(db, resume_id, current_user)


@app.delete("/resumes/{resume_id}", status_code=204)
def delete_resume(
    resume_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    resume = _owned_resume(db, resume_id, current_user)
    db.delete(resume)
    db.commit()
    return


@app.post("/resumes/{resume_id}/duplicate", response_model=schemas.ResumeResponse, status_code=201)
def duplicate_resume(
    resume_id: int,
    payload: schemas.ResumeDuplicateRequest = Body(default=None),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Clone a resume (and all of its sections) to tailor it for a job application."""
    source = _full_resume(db, resume_id, current_user)
    payload = payload or schemas.ResumeDuplicateRequest()

    base_title = source.title or source.full_name
    clone = models.Resume(
        title=payload.title or f"{base_title} - Copy",
        full_name=payload.full_name or source.full_name,
        email=source.email,
        phone=source.phone,
        location=source.location,
        linkedin_url=source.linkedin_url,
        github_url=source.github_url,
        portfolio_url=source.portfolio_url,
        summary=payload.summary if payload.summary is not None else source.summary,
        user_id=current_user.id,
    )
    clone.experiences = [
        models.Experience(
            company=exp.company,
            role=exp.role,
            years=exp.years,
            location=exp.location,
            start_date=exp.start_date,
            end_date=exp.end_date,
            is_current=exp.is_current,
            description=exp.description,
            bullets=list(exp.bullets or []),
            sort_order=exp.sort_order,
        )
        for exp in source.experiences
    ]
    clone.educations = [
        models.Education(
            institution=edu.institution,
            degree=edu.degree,
            field_of_study=edu.field_of_study,
            location=edu.location,
            start_date=edu.start_date,
            end_date=edu.end_date,
            is_current=edu.is_current,
            gpa=edu.gpa,
            honors_coursework=edu.honors_coursework,
            sort_order=edu.sort_order,
        )
        for edu in source.educations
    ]
    clone.skills = [
        models.Skill(
            category=skill.category,
            name=skill.name,
            sort_order=skill.sort_order,
        )
        for skill in source.skills
    ]
    clone.projects = [
        models.Project(
            title=project.title,
            tech_stack=list(project.tech_stack or []),
            description=project.description,
            bullets=list(project.bullets or []),
            github_url=project.github_url,
            live_url=project.live_url,
            sort_order=project.sort_order,
        )
        for project in source.projects
    ]

    db.add(clone)
    db.commit()
    return _full_resume(db, clone.id, current_user)


# ------------------------------------------------------------------
# Education
# ------------------------------------------------------------------
@app.post(
    "/resumes/{resume_id}/educations",
    response_model=schemas.EducationResponse,
    status_code=201,
)
def create_education(
    resume_id: int,
    payload: schemas.EducationCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    resume = _owned_resume(db, resume_id, current_user)
    education = models.Education(**_education_kwargs(payload), resume_id=resume.id)
    db.add(education)
    db.commit()
    db.refresh(education)
    return education


@app.get(
    "/resumes/{resume_id}/educations",
    response_model=list[schemas.EducationResponse],
)
def list_educations(
    resume_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    resume = _owned_resume(db, resume_id, current_user)
    return (
        db.query(models.Education)
        .filter(models.Education.resume_id == resume.id)
        .order_by(models.Education.sort_order, models.Education.id)
        .all()
    )


@app.patch(
    "/resumes/{resume_id}/educations/{edu_id}",
    response_model=schemas.EducationResponse,
)
def update_education(
    resume_id: int,
    edu_id: int,
    payload: schemas.EducationUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    resume = _owned_resume(db, resume_id, current_user)
    education = _child_or_404(db, models.Education, edu_id, resume.id)
    _apply_update(education, payload, models.Education)
    db.commit()
    db.refresh(education)
    return education


@app.delete("/resumes/{resume_id}/educations/{edu_id}", status_code=204)
def delete_education(
    resume_id: int,
    edu_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    resume = _owned_resume(db, resume_id, current_user)
    education = _child_or_404(db, models.Education, edu_id, resume.id)
    db.delete(education)
    db.commit()
    return


# ------------------------------------------------------------------
# Experience
# ------------------------------------------------------------------
@app.post(
    "/resumes/{resume_id}/experiences",
    response_model=schemas.ExperienceResponse,
    status_code=201,
)
def create_experience(
    resume_id: int,
    payload: schemas.ExperienceCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    resume = _owned_resume(db, resume_id, current_user)
    experience = models.Experience(**_experience_kwargs(payload), resume_id=resume.id)
    db.add(experience)
    db.commit()
    db.refresh(experience)
    return experience


@app.get(
    "/resumes/{resume_id}/experiences",
    response_model=list[schemas.ExperienceResponse],
)
def list_experiences(
    resume_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    resume = _owned_resume(db, resume_id, current_user)
    return (
        db.query(models.Experience)
        .filter(models.Experience.resume_id == resume.id)
        .order_by(models.Experience.sort_order, models.Experience.id)
        .all()
    )


@app.patch(
    "/resumes/{resume_id}/experiences/{exp_id}",
    response_model=schemas.ExperienceResponse,
)
def update_experience(
    resume_id: int,
    exp_id: int,
    payload: schemas.ExperienceUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    resume = _owned_resume(db, resume_id, current_user)
    experience = _child_or_404(db, models.Experience, exp_id, resume.id)
    _apply_update(experience, payload, models.Experience)
    db.commit()
    db.refresh(experience)
    return experience


@app.delete("/resumes/{resume_id}/experiences/{exp_id}", status_code=204)
def delete_experience(
    resume_id: int,
    exp_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    resume = _owned_resume(db, resume_id, current_user)
    experience = _child_or_404(db, models.Experience, exp_id, resume.id)
    db.delete(experience)
    db.commit()
    return


# ------------------------------------------------------------------
# Skills
# ------------------------------------------------------------------
@app.post(
    "/resumes/{resume_id}/skills",
    response_model=list[schemas.SkillResponse],
    status_code=201,
)
def create_skills(
    resume_id: int,
    payload: schemas.SkillCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Add one skill (`name`) and/or a batch of skills (`names`) to a category."""
    resume = _owned_resume(db, resume_id, current_user)

    names: list[str] = list(payload.names)
    if payload.name:
        names.append(payload.name)
    if not names:
        raise HTTPException(status_code=422, detail="Provide 'name' or 'names'")
    if len(names) > 50:
        raise HTTPException(status_code=422, detail="Too many skills in one request")

    existing = {
        skill.name.lower()
        for skill in db.query(models.Skill).filter(
            models.Skill.resume_id == resume.id,
            func.lower(models.Skill.category) == payload.category.lower(),
        )
    }

    created: list[models.Skill] = []
    seen: set[str] = set()
    for name in names:
        clean = name.strip()
        key = clean.lower()
        if not clean or key in existing or key in seen:
            continue
        seen.add(key)
        skill = models.Skill(
            category=payload.category,
            name=clean,
            sort_order=payload.sort_order,
            resume_id=resume.id,
        )
        db.add(skill)
        created.append(skill)

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=400, detail="Duplicate skill in this category")

    for skill in created:
        db.refresh(skill)
    return created


@app.get(
    "/resumes/{resume_id}/skills",
    response_model=list[schemas.SkillCategoryResponse],
)
def list_skills(
    resume_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Skills grouped by category."""
    resume = _owned_resume(db, resume_id, current_user)
    skills = (
        db.query(models.Skill)
        .filter(models.Skill.resume_id == resume.id)
        .order_by(models.Skill.category, models.Skill.sort_order, models.Skill.id)
        .all()
    )

    grouped: dict[str, list[models.Skill]] = {}
    for skill in skills:
        grouped.setdefault(skill.category, []).append(skill)

    return [
        schemas.SkillCategoryResponse(category=category, skills=items)
        for category, items in grouped.items()
    ]


@app.patch(
    "/resumes/{resume_id}/skills/{skill_id}",
    response_model=schemas.SkillResponse,
)
def update_skill(
    resume_id: int,
    skill_id: int,
    payload: schemas.SkillUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    resume = _owned_resume(db, resume_id, current_user)
    skill = _child_or_404(db, models.Skill, skill_id, resume.id)
    _apply_update(skill, payload, models.Skill)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=400, detail="Duplicate skill in this category")
    db.refresh(skill)
    return skill


@app.delete("/resumes/{resume_id}/skills/{skill_id}", status_code=204)
def delete_skill(
    resume_id: int,
    skill_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    resume = _owned_resume(db, resume_id, current_user)
    skill = _child_or_404(db, models.Skill, skill_id, resume.id)
    db.delete(skill)
    db.commit()
    return


# ------------------------------------------------------------------
# Projects
# ------------------------------------------------------------------
@app.post(
    "/resumes/{resume_id}/projects",
    response_model=schemas.ProjectResponse,
    status_code=201,
)
def create_project(
    resume_id: int,
    payload: schemas.ProjectCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    resume = _owned_resume(db, resume_id, current_user)
    project = models.Project(**_project_kwargs(payload), resume_id=resume.id)
    db.add(project)
    db.commit()
    db.refresh(project)
    return project


@app.get(
    "/resumes/{resume_id}/projects",
    response_model=list[schemas.ProjectResponse],
)
def list_projects(
    resume_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    resume = _owned_resume(db, resume_id, current_user)
    return (
        db.query(models.Project)
        .filter(models.Project.resume_id == resume.id)
        .order_by(models.Project.sort_order, models.Project.id)
        .all()
    )


@app.patch(
    "/resumes/{resume_id}/projects/{project_id}",
    response_model=schemas.ProjectResponse,
)
def update_project(
    resume_id: int,
    project_id: int,
    payload: schemas.ProjectUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    resume = _owned_resume(db, resume_id, current_user)
    project = _child_or_404(db, models.Project, project_id, resume.id)
    _apply_update(project, payload, models.Project)
    db.commit()
    db.refresh(project)
    return project


@app.delete("/resumes/{resume_id}/projects/{project_id}", status_code=204)
def delete_project(
    resume_id: int,
    project_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    resume = _owned_resume(db, resume_id, current_user)
    project = _child_or_404(db, models.Project, project_id, resume.id)
    db.delete(project)
    db.commit()
    return


# ------------------------------------------------------------------
# AI
# ------------------------------------------------------------------
def _groq_chat(prompt: str, temperature: float = 0.7) -> str:
    """Send a prompt to Groq and return the raw completion text."""
    if not groq_client:
        raise HTTPException(
            status_code=500,
            detail="GROQ_API_KEY is not configured on the server."
        )

    model_name = os.getenv("GROQ_MODEL", "qwen/qwen3.8-27b")
    try:
        chat_completion = groq_client.chat.completions.create(
            messages=[{"role": "user", "content": prompt}],
            model=model_name,
            temperature=temperature,
        )
        return chat_completion.choices[0].message.content.strip()
    except groq.RateLimitError:
        raise HTTPException(
            status_code=429,
            detail="AI rate limit reached. Please wait a few seconds and try again."
        )
    except groq.APIError as e:
        raise HTTPException(
            status_code=502,
            detail=f"AI provider error: {getattr(e, 'message', str(e))}"
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


def _parse_bullet_variants(raw: str) -> list[str]:
    """Parse the AI response into a clean list of bullet strings."""
    text = raw.strip()
    text = re.sub(r"^```(?:json)?", "", text, flags=re.IGNORECASE).strip()
    text = re.sub(r"```$", "", text).strip()

    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        data = None

    if isinstance(data, dict):
        data = data.get("variants") or data.get("bullets") or data.get("results") or []

    if isinstance(data, list):
        variants = [str(item).strip() for item in data if str(item).strip()]
        if variants:
            return variants[:3]

    # Fallback: treat the answer as plain lines of bullets.
    lines = []
    for line in text.splitlines():
        cleaned = re.sub(r"^\s*(?:[-*•]|\d+[.)])\s*", "", line).strip()
        if cleaned and cleaned.startswith(("{", "[")):
            continue
        if cleaned:
            lines.append(cleaned)
    return lines[:3]


@app.post("/ai/generate-summary", response_model=schemas.AIGenerateResponse)
def generate_summary(
    payload: schemas.AIGenerateRequest,
    current_user: models.User = Depends(get_current_user)
):
    # 1. Build the prompt for the AI
    exp_lines = []
    for e in payload.experiences:
        role = e.get("role", "Role")
        company = e.get("company", "Company")
        years = e.get("years", "N/A")
        exp_lines.append(f"- {role} at {company} for {years} years")

    exp_text = "\n".join(exp_lines)

    prompt = f"""
    You are an expert resume writer.
    Write a professional, 3-sentence summary for {payload.full_name}.
    Here is their experience:
    {exp_text if exp_text else "No experience added yet."}

    Here is their current draft (improve it):
    "{payload.current_summary}"

    Return ONLY the summary text, no quotes, no extra formatting.
    """

    # 2. Call the AI
    ai_text = _groq_chat(prompt, temperature=0.7)
    return {"summary": ai_text}


@app.post("/ai/rewrite-bullet-xyz", response_model=schemas.AIRewriteBulletResponse)
def rewrite_bullet_xyz(
    payload: schemas.AIRewriteBulletRequest,
    current_user: models.User = Depends(get_current_user),
):
    """Turn a rough duty/accomplishment into high-impact Google-style XYZ bullets."""
    context_lines = []
    if payload.role:
        context_lines.append(f"- Candidate's role: {payload.role}")
    if payload.company:
        context_lines.append(f"- Company: {payload.company}")
    if payload.target_role:
        context_lines.append(f"- Target role for this resume: {payload.target_role}")
    if payload.tone:
        context_lines.append(f"- Tone: {payload.tone}")
    context = "\n".join(context_lines) if context_lines else "- No extra context provided."

    prompt = f"""
    You are an expert technical resume writer following Google's official "XYZ" resume formula.

    Formula: "{XYZ_FORMULA}"
      X = the accomplishment (what was achieved) -> strong action verb + result
      Y = the metric or scale of impact (numbers, %, time saved, users, revenue, etc.)
      Z = the method (how it was done) -> tools, processes, techniques

    Task: rewrite the rough bullet below into 2-3 distinct, high-impact resume bullets
    that follow the XYZ formula.

    Rough bullet: "{payload.bullet}"

    Context:
    {context}

    Rules:
    - Start every bullet with a strong past-tense action verb (e.g. Led, Built, Reduced, Delivered, Designed).
    - Prefer a concrete, measurable outcome for [Y]. If no metric is given, insert a bracketed
      placeholder such as [25%] or [\$100k] instead of inventing a fake number.
    - Keep each bullet to one line and under 25 words. No first-person pronouns.
    - Do not invent employers, job titles, technologies or dates that were not provided.
    - Vary the wording between the variants so the candidate can pick one.

    Return ONLY a JSON array of 2-3 strings, for example:
    ["Reworked the checkout query path, cutting p95 latency by 40% by adding composite indexes.",
     "Led the migration of 12 legacy tables, reducing nightly ETL failures to zero by adding idempotent upserts."]
    """

    raw = _groq_chat(prompt, temperature=0.8)
    variants = _parse_bullet_variants(raw)
    if not variants:
        raise HTTPException(
            status_code=502,
            detail="The AI did not return any usable bullet variants. Please try again.",
        )
    return {"variants": variants, "formula": XYZ_FORMULA}
