from datetime import datetime, timezone
from fastapi import Body, Depends, FastAPI, HTTPException, Query
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload
from jose import JWTError, jwt
from dotenv import load_dotenv
import groq
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
        "bullets": list(getattr(payload, "bullets", []) or []),
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
        "tech_stack": list(getattr(payload, "tech_stack", []) or []),
        "description": payload.description,
        "bullets": list(getattr(payload, "bullets", []) or []),
        "github_url": payload.github_url,
        "live_url": payload.live_url,
        "sort_order": payload.sort_order,
    }


def _skill_models(skill_payloads: list[schemas.SkillCreate]) -> list[models.Skill]:
    """Convert a list of SkillCreate (with single name or batch names) into Skill models."""
    created: list[models.Skill] = []
    seen: set[tuple[str, str]] = set()
    for item in skill_payloads:
        names = list(item.names)
        if item.name:
            names.append(item.name)
        for name in names:
            clean = name.strip()
            key = (item.category.strip().lower(), clean.lower())
            if not clean or key in seen:
                continue
            seen.add(key)
            created.append(
                models.Skill(
                    category=item.category.strip(),
                    name=clean,
                    sort_order=item.sort_order,
                )
            )
    return created


def _clone_resume(
    source: models.Resume,
    user_id: int,
    title: str | None = None,
    full_name: str | None = None,
    summary: str | None = None,
) -> models.Resume:
    """Create an independent copy of a resume and all its sections."""
    base_title = source.title or source.full_name
    clone = models.Resume(
        title=title or f"{base_title} - Copy",
        full_name=full_name or source.full_name,
        email=source.email,
        phone=source.phone,
        location=source.location,
        linkedin_url=source.linkedin_url,
        github_url=source.github_url,
        portfolio_url=source.portfolio_url,
        summary=summary if summary is not None else source.summary,
        user_id=user_id,
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
    seen_skills = set()
    clone_skills = []
    for skill in source.skills:
        key = (skill.category.lower(), skill.name.lower())
        if key in seen_skills:
            continue
        seen_skills.add(key)
        clone_skills.append(
            models.Skill(
                category=skill.category,
                name=skill.name,
                sort_order=skill.sort_order,
            )
        )
    clone.skills = clone_skills
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
    return clone


def _owned_application(
    db: Session, application_id: int, user: models.User
) -> models.Application:
    application = db.query(models.Application).filter(
        models.Application.id == application_id,
        models.Application.user_id == user.id,
    ).first()
    if application is None:
        raise HTTPException(status_code=404, detail="Application not found")
    return application


def _application_detail(
    db: Session, application: models.Application, user: models.User
) -> dict:
    result = schemas.ApplicationSummary.model_validate(application).model_dump()
    result["job_description"] = application.job_description
    result["tailored_resume"] = _full_resume(
        db, application.tailored_resume_id, user
    )
    return result



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


@app.get("/users/me", response_model=schemas.UserResponse)
@app.get("/me", response_model=schemas.UserResponse)
def get_current_user_profile(
    current_user: models.User = Depends(get_current_user),
):
    """Return the profile of the authenticated user."""
    return current_user


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
    resume.skills = _skill_models(payload.skills)
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
    if db.query(models.Application).filter(
        models.Application.tailored_resume_id == resume.id
    ).first():
        raise HTTPException(
            status_code=409,
            detail="This resume is an application's saved version. Delete the application first.",
        )
    db.query(models.Application).filter(
        models.Application.source_resume_id == resume.id
    ).update({"source_resume_id": None}, synchronize_session=False)
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
    clone = _clone_resume(
        source,
        current_user.id,
        title=payload.title,
        full_name=payload.full_name,
        summary=payload.summary,
    )

    db.add(clone)
    db.commit()
    return _full_resume(db, clone.id, current_user)


@app.post(
    "/applications",
    response_model=schemas.ApplicationDetail,
    status_code=201,
)
def create_application(
    payload: schemas.ApplicationCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    source = _full_resume(db, payload.resume_id, current_user)
    resume_title = f"{payload.company} - {payload.role}"[:150]
    tailored_resume = _clone_resume(source, current_user.id, title=resume_title)
    db.add(tailored_resume)
    try:
        db.flush()
        application = models.Application(
            company=payload.company,
            role=payload.role,
            job_url=payload.job_url,
            job_description=payload.job_description,
            notes=payload.notes,
            user_id=current_user.id,
            source_resume_id=source.id,
            tailored_resume_id=tailored_resume.id,
        )
        db.add(application)
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=400, detail="Could not create application")

    db.refresh(application)
    return _application_detail(db, application, current_user)


@app.get("/applications", response_model=list[schemas.ApplicationSummary])
def list_applications(
    status: schemas.ApplicationStatus | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    query = db.query(models.Application).filter(
        models.Application.user_id == current_user.id
    )
    if status is not None:
        query = query.filter(models.Application.status == status)
    return query.order_by(
        models.Application.updated_at.desc(),
        models.Application.id.desc(),
    ).offset(offset).limit(limit).all()


@app.get(
    "/applications/{application_id}",
    response_model=schemas.ApplicationDetail,
)
def get_application(
    application_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    application = _owned_application(db, application_id, current_user)
    return _application_detail(db, application, current_user)


@app.patch(
    "/applications/{application_id}",
    response_model=schemas.ApplicationDetail,
)
def update_application(
    application_id: int,
    payload: schemas.ApplicationUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    application = _owned_application(db, application_id, current_user)
    _apply_update(application, payload, models.Application)
    db.commit()
    return _application_detail(db, application, current_user)


@app.delete("/applications/{application_id}", status_code=204)
def delete_application(
    application_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    application = _owned_application(db, application_id, current_user)
    tailored_resume = _owned_resume(
        db, application.tailored_resume_id, current_user
    )
    db.query(models.Application).filter(
        models.Application.source_resume_id == tailored_resume.id
    ).update({"source_resume_id": None}, synchronize_session=False)
    db.delete(application)
    db.flush()
    db.delete(tailored_resume)
    db.commit()
    return


@app.patch("/resumes/{resume_id}/reorder", status_code=200)
def reorder_section(
    resume_id: int,
    payload: schemas.ReorderRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Batch reorder items in a section (experiences, educations, skills, or projects)."""
    resume = _owned_resume(db, resume_id, current_user)
    model_map = {
        "experiences": models.Experience,
        "educations": models.Education,
        "skills": models.Skill,
        "projects": models.Project,
    }
    model_cls = model_map[payload.section]

    for order, item_id in enumerate(payload.ids):
        db.query(model_cls).filter(
            model_cls.id == item_id,
            model_cls.resume_id == resume.id,
        ).update({"sort_order": order})

    db.commit()
    return {"message": f"{payload.section.capitalize()} reordered successfully"}


@app.get("/resumes/{resume_id}/export/json")
def export_json_resume(
    resume_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Export the resume following the official JSON Resume open standard (jsonresume.org)."""
    resume = _full_resume(db, resume_id, current_user)

    profiles = []
    if resume.linkedin_url:
        profiles.append({"network": "LinkedIn", "url": resume.linkedin_url})
    if resume.github_url:
        profiles.append({"network": "GitHub", "url": resume.github_url})
    if resume.portfolio_url:
        profiles.append({"network": "Portfolio", "url": resume.portfolio_url})

    skills_map = {}
    for skill in resume.skills:
        skills_map.setdefault(skill.category, []).append(skill.name)

    return {
        "basics": {
            "name": resume.full_name,
            "label": resume.title or "",
            "email": resume.email,
            "phone": resume.phone or "",
            "summary": resume.summary or "",
            "location": {"city": resume.location or ""},
            "profiles": profiles,
        },
        "work": [
            {
                "name": exp.company,
                "position": exp.role,
                "location": exp.location or "",
                "startDate": exp.start_date or "",
                "endDate": exp.end_date or ("Present" if exp.is_current else ""),
                "summary": exp.description or "",
                "highlights": list(exp.bullets or []),
            }
            for exp in resume.experiences
        ],
        "education": [
            {
                "institution": edu.institution,
                "area": edu.field_of_study or "",
                "studyType": edu.degree,
                "startDate": edu.start_date or "",
                "endDate": edu.end_date or ("Present" if edu.is_current else ""),
                "score": edu.gpa or "",
                "courses": [
                    c.strip() for c in edu.honors_coursework.split(",") if c.strip()
                ]
                if edu.honors_coursework
                else [],
            }
            for edu in resume.educations
        ],
        "skills": [
            {"name": category, "keywords": keywords}
            for category, keywords in skills_map.items()
        ],
        "projects": [
            {
                "name": p.title,
                "description": p.description or "",
                "highlights": list(p.bullets or []),
                "keywords": list(p.tech_stack or []),
                "url": p.live_url or p.github_url or "",
            }
            for p in resume.projects
        ],
    }


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


@app.get(
    "/resumes/{resume_id}/educations/{edu_id}",
    response_model=schemas.EducationResponse,
)
def get_education(
    resume_id: int,
    edu_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    resume = _owned_resume(db, resume_id, current_user)
    return _child_or_404(db, models.Education, edu_id, resume.id)


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


@app.get(
    "/resumes/{resume_id}/experiences/{exp_id}",
    response_model=schemas.ExperienceResponse,
)
def get_experience(
    resume_id: int,
    exp_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    resume = _owned_resume(db, resume_id, current_user)
    return _child_or_404(db, models.Experience, exp_id, resume.id)


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


@app.get(
    "/resumes/{resume_id}/skills/{skill_id}",
    response_model=schemas.SkillResponse,
)
def get_skill(
    resume_id: int,
    skill_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    resume = _owned_resume(db, resume_id, current_user)
    return _child_or_404(db, models.Skill, skill_id, resume.id)


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


@app.get(
    "/resumes/{resume_id}/projects/{project_id}",
    response_model=schemas.ProjectResponse,
)
def get_project(
    resume_id: int,
    project_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    resume = _owned_resume(db, resume_id, current_user)
    return _child_or_404(db, models.Project, project_id, resume.id)


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
      placeholder such as [25%] or [$100k] instead of inventing a fake number.
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


@app.post("/ai/tailor-resume", response_model=schemas.AITailorResponse)
def tailor_resume(
    payload: schemas.AITailorRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Analyze a resume against a target job description for ATS matching and improvements."""
    return _tailor_resume_analysis(payload, db, current_user)


def _tailor_resume_analysis(
    payload: schemas.AITailorRequest,
    db: Session,
    current_user: models.User,
) -> schemas.AITailorResponse:
    resume_content = ""
    if payload.resume_id:
        resume = _full_resume(db, payload.resume_id, current_user)
        lines = [
            f"Candidate: {resume.full_name} ({resume.title or 'N/A'})",
            f"Summary: {resume.summary or 'N/A'}",
            "Experience:",
        ]
        for exp in resume.experiences:
            lines.append(
                f"- {exp.role} at {exp.company} ({exp.start_date or ''} - {exp.end_date or ''}): {exp.description}"
            )
            for b in (exp.bullets or []):
                lines.append(f"  * {b}")
        lines.append("Skills:")
        for sk in resume.skills:
            lines.append(f"- {sk.category}: {sk.name}")
        lines.append("Projects:")
        for pr in resume.projects:
            lines.append(
                f"- {pr.title} (Stack: {', '.join(pr.tech_stack or [])}): {pr.description}"
            )
        lines.append("Education:")
        for ed in resume.educations:
            lines.append(f"- {ed.degree} in {ed.field_of_study or ''} from {ed.institution}")
        resume_content = "\n".join(lines)
    elif payload.resume_text:
        resume_content = payload.resume_text
    else:
        raise HTTPException(
            status_code=422,
            detail="Provide either 'resume_id' or 'resume_text' to analyze."
        )

    prompt = f"""
    You are an expert technical recruiter and ATS (Applicant Tracking System) specialist.
    Analyze the following resume against the target job description.

    Target Role: {payload.target_role or "Not specified"}

    Job Description:
    {payload.job_description}

    Resume Content:
    {resume_content}

    Evaluate:
    1. match_score: An integer from 0 to 100 based on keyword overlap, role alignment, and required skills.
    2. matched_skills: Array of relevant skills/keywords found in BOTH the job description and the resume.
    3. missing_skills: Array of critical skills, tools, or qualifications mentioned in the job description that are MISSING from the resume.
    4. strengths: Array of 2-3 specific strengths in this candidate's profile for this job.
    5. recommendations: Array of 3-4 concrete, actionable suggestions to improve the resume for this job (e.g. emphasizing specific experiences or framing bullets with Google's X-Y-Z formula).

    Return ONLY a valid JSON object matching this structure:
    {{
      "match_score": 85,
      "matched_skills": ["Python", "Docker", "FastAPI"],
      "missing_skills": ["Kubernetes", "GraphQL"],
      "strengths": ["Strong backend development experience", "Demonstrated measurable performance improvements"],
      "recommendations": ["Highlight distributed systems experience in the latest role", "Include metrics on query optimization"]
    }}
    """

    raw = _groq_chat(prompt, temperature=0.3)
    text = raw.strip()
    text = re.sub(r"^```(?:json)?", "", text, flags=re.IGNORECASE).strip()
    text = re.sub(r"```$", "", text).strip()

    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        raise HTTPException(
            status_code=502,
            detail="The AI returned an invalid resume analysis. Please try again.",
        )

    try:
        return schemas.AITailorResponse.model_validate(data)
    except (ValueError, TypeError):
        raise HTTPException(
            status_code=502,
            detail="The AI returned an invalid resume analysis. Please try again.",
        )


@app.post(
    "/applications/{application_id}/analyze",
    response_model=schemas.ApplicationDetail,
)
def analyze_application(
    application_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    application = _owned_application(db, application_id, current_user)
    analysis = _tailor_resume_analysis(
        schemas.AITailorRequest(
            resume_id=application.tailored_resume_id,
            job_description=application.job_description,
            target_role=application.role,
        ),
        db,
        current_user,
    )
    application.analysis = analysis.model_dump()
    application.match_score = analysis.match_score
    application.analyzed_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(application)
    return _application_detail(db, application, current_user)


@app.post("/ai/suggest-skills", response_model=schemas.AISuggestSkillsResponse)
def suggest_skills(
    payload: schemas.AISuggestSkillsRequest,
    current_user: models.User = Depends(get_current_user),
):
    """Suggest categorized industry-standard skills for a target job title following Google standards."""
    prompt = f"""
    You are an expert technical career advisor.
    Provide the most in-demand, industry-standard hard skills for the role: "{payload.job_title}".
    Industry: "{payload.industry or 'Technology'}".

    Categorize them into 3-5 standard categories such as:
    - Languages
    - Frameworks & Libraries
    - Cloud, DevOps & Infrastructure
    - Databases & Storage
    - Tools & Methodologies

    Only include concrete hard skills (no vague soft skills like "teamwork" or "problem solving").
    Return ONLY a valid JSON object mapping category names to arrays of 4-6 skill strings:
    {{
      "Languages": ["Python", "TypeScript", "SQL"],
      "Frameworks & Libraries": ["FastAPI", "React", "Next.js"],
      "Cloud, DevOps & Infrastructure": ["Docker", "AWS", "CI/CD pipelines"],
      "Databases & Storage": ["PostgreSQL", "Redis", "Elasticsearch"]
    }}
    """

    raw = _groq_chat(prompt, temperature=0.5)
    text = raw.strip()
    text = re.sub(r"^```(?:json)?", "", text, flags=re.IGNORECASE).strip()
    text = re.sub(r"```$", "", text).strip()

    try:
        data = json.loads(text)
        if isinstance(data, dict):
            clean_dict = {
                str(k): [str(i) for i in v if str(i).strip()]
                for k, v in data.items()
                if isinstance(v, list)
            }
            return schemas.AISuggestSkillsResponse(
                job_title=payload.job_title,
                suggested_skills=clean_dict,
            )
    except Exception:
        pass

    return schemas.AISuggestSkillsResponse(
        job_title=payload.job_title,
        suggested_skills={
            "Core Skills": ["Python", "Git", "REST APIs", "SQL"]
        },
    )
