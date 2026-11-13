import sys
from pathlib import Path

_backend_src = str(Path(__file__).resolve().parent)
if _backend_src not in sys.path:
    sys.path.insert(0, _backend_src)

from datetime import datetime, timezone
from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload
from jose import JWTError, jwt
from dotenv import load_dotenv
from xml.sax.saxutils import escape
import groq
from groq import Groq
import io
import json
import logging
import os
import re
import time
import unicodedata
import zipfile
from collections import defaultdict, deque

load_dotenv()

logger = logging.getLogger(__name__)

import models
import schemas
import database
from database import engine
from auth import hash_password, verify_password, create_access_token, SECRET_KEY, ALGORITHM
from fastapi.middleware.cors import CORSMiddleware
from migrations import run_migrations

# Vercel imports the ASGI app during function startup. Keep initialization
# best-effort so a temporary database outage does not make the whole function
# impossible to import; requests will report the underlying database error.
try:
    models.Base.metadata.create_all(bind=engine)
    run_migrations(engine)
except Exception:
    logger.exception(
        "Database initialization failed. Check DATABASE_URL / database availability."
    )

APP_ENV = os.getenv("APP_ENV", "development").lower()
app = FastAPI(
    title="Resume Builder API",
    docs_url=None if APP_ENV in {"production", "prod"} else "/docs",
    redoc_url=None if APP_ENV in {"production", "prod"} else "/redoc",
)

_groq_client = None


def get_groq_client():
    global _groq_client
    if _groq_client is not None:
        return _groq_client
    groq_api_key = os.getenv("GROQ_API_KEY")
    if groq_api_key:
        _groq_client = Groq(api_key=groq_api_key)
    return _groq_client

configured_frontend_origins = [
    origin.strip()
    for origin in os.getenv(
        "FRONTEND_ORIGINS",
        "http://localhost:5173,http://127.0.0.1:5173",
    ).split(",")
    if origin.strip()
]
frontend_origins = list(dict.fromkeys([
    *configured_frontend_origins,
    "https://hussainmurtaza5.github.io",
]))
if "*" in frontend_origins:
    raise RuntimeError("FRONTEND_ORIGINS cannot use '*' with credentialed requests")

app.add_middleware(
    CORSMiddleware,
    allow_origins=frontend_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)


class SecurityHeadersMiddleware:
    """Add browser hardening headers to every API response."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def secure_send(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.extend([
                    (b"x-content-type-options", b"nosniff"),
                    (b"x-frame-options", b"DENY"),
                    (b"referrer-policy", b"no-referrer"),
                    (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
                    (b"content-security-policy", b"default-src 'none'; frame-ancestors 'none'"),
                ])
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, secure_send)


class RateLimitMiddleware:
    """Small in-process limiter for single-instance/dev deployments.

    For multi-instance Vercel production, put an edge/provider limiter in front
    of the API as well; this still protects each warm function instance.
    """

    def __init__(self, app):
        self.app = app
        self.events = defaultdict(deque)
        self.rules = {
            "auth": (10, 60),
            "ai": (20, 60),
            "default": (120, 60),
        }

    def _rule_for(self, path):
        if path in {"/login", "/register"}:
            return "auth"
        if path.startswith("/ai/") or path.endswith("/analyze"):
            return "ai"
        return "default"

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("method") == "OPTIONS":
            await self.app(scope, receive, send)
            return

        request = Request(scope)
        path = request.url.path
        if path in {"/", "/health"}:
            await self.app(scope, receive, send)
            return

        # Vercel provides this header at the trusted edge. Use the
        # socket address as the fallback for local/dev requests.
        forwarded = request.headers.get("x-forwarded-for")
        client = (forwarded.split(",")[0].strip() if forwarded else None) or (
            request.client.host if request.client else "unknown"
        )
        rule_name = self._rule_for(path)
        limit, window = self.rules[rule_name]
        key = f"{client}:{rule_name}"
        now = time.monotonic()
        retry_after = None
        bucket = self.events[key]
        while bucket and now - bucket[0] >= window:
            bucket.popleft()
        if len(bucket) >= limit:
            retry_after = max(1, int(window - (now - bucket[0])))
        else:
            bucket.append(now)

        if retry_after is not None:
            response = JSONResponse(
                status_code=429,
                content={"detail": "Too many requests. Please try again later."},
                headers={"Retry-After": str(retry_after)},
            )
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)


app.add_middleware(RateLimitMiddleware)
app.add_middleware(SecurityHeadersMiddleware)

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/login")

XYZ_FORMULA = "Accomplished [X], as measured by [Y], by doing [Z]"


def get_db():
    db = database.SessionLocal()
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
        payload = jwt.decode(token, str(SECRET_KEY), algorithms=[ALGORITHM])
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


def _sanitize_word_text(value: str) -> str:
    return unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode("ascii")


def _resume_document_lines(resume: models.Resume) -> list[str]:
    """Return a line-based representation suitable for DOCX/PDF export."""
    lines: list[str] = []

    if resume.full_name:
        lines.append(resume.full_name)
    if resume.title:
        lines.append(resume.title)

    contact_parts = [p for p in [resume.email, resume.phone, resume.location] if p]
    if contact_parts:
        lines.append(" | ".join(contact_parts))

    if resume.summary:
        lines.extend(["", "Summary", resume.summary.strip()])

    if resume.experiences:
        lines.extend(["", "Experience"])
        for exp in resume.experiences:
            header = f"{exp.role} @ {exp.company}"
            if exp.location:
                header += f" — {exp.location}"
            if exp.start_date or exp.end_date or exp.is_current:
                header += f" ({exp.start_date or 'Present'} - {exp.end_date or ('Present' if exp.is_current else '')})"
            lines.append(header)
            if exp.description:
                lines.append(exp.description.strip())
            for bullet in exp.bullets or []:
                lines.append(f"• {bullet.strip()}")

    if resume.educations:
        lines.extend(["", "Education"])
        for edu in resume.educations:
            header = f"{edu.degree} | {edu.institution}"
            if edu.location:
                header += f" — {edu.location}"
            if edu.start_date or edu.end_date or edu.is_current:
                header += f" ({edu.start_date or 'Present'} - {edu.end_date or ('Present' if edu.is_current else '')})"
            lines.append(header)
            if edu.field_of_study:
                lines.append(edu.field_of_study)
            if edu.gpa:
                lines.append(f"GPA: {edu.gpa}")
            if edu.honors_coursework:
                lines.append(f"Honors: {edu.honors_coursework}")

    if resume.skills:
        lines.extend(["", "Skills"])
        grouped: dict[str, list[str]] = {}
        for skill in resume.skills:
            grouped.setdefault(skill.category, []).append(skill.name)
        for category, names in grouped.items():
            lines.append(f"{category}: {', '.join(names)}")

    if resume.projects:
        lines.extend(["", "Projects"])
        for project in resume.projects:
            lines.append(project.title)
            if project.description:
                lines.append(project.description.strip())
            if project.tech_stack:
                lines.append(f"Tech: {', '.join(project.tech_stack)}")
            for bullet in project.bullets or []:
                lines.append(f"• {bullet.strip()}")

    if resume.linkedin_url or resume.github_url or resume.portfolio_url:
        lines.extend(["", "Links"])
        for url, label in [
            (resume.linkedin_url, "LinkedIn"),
            (resume.github_url, "GitHub"),
            (resume.portfolio_url, "Portfolio"),
        ]:
            if url:
                lines.append(f"{label}: {url}")

    return [line for line in lines if line is not None and str(line).strip()]


def _build_pdf_resume_bytes(resume: models.Resume) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.platypus import (
        HRFlowable,
        KeepTogether,
        Paragraph,
        SimpleDocTemplate,
        Spacer,
    )

    buffer = io.BytesIO()
    document = SimpleDocTemplate(
        buffer,
        pagesize=letter,
        rightMargin=0.7 * inch,
        leftMargin=0.7 * inch,
        topMargin=0.65 * inch,
        bottomMargin=0.65 * inch,
        title=resume.title or f"{resume.full_name} - Resume",
        author=resume.full_name,
    )
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(
        name="ResumeName",
        parent=styles["Title"],
        fontName="Helvetica-Bold",
        fontSize=22,
        leading=26,
        textColor=colors.HexColor("#17324D"),
        alignment=TA_CENTER,
        spaceAfter=3,
    ))
    styles.add(ParagraphStyle(
        name="ResumeSubtitle",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=10,
        leading=14,
        textColor=colors.HexColor("#526477"),
        alignment=TA_CENTER,
        spaceAfter=6,
    ))
    styles.add(ParagraphStyle(
        name="ResumeSection",
        parent=styles["Heading2"],
        fontName="Helvetica-Bold",
        fontSize=12,
        leading=15,
        textColor=colors.HexColor("#17324D"),
        spaceBefore=11,
        spaceAfter=4,
        keepWithNext=True,
    ))
    styles.add(ParagraphStyle(
        name="ResumeEntry",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=10,
        leading=13,
        textColor=colors.HexColor("#243746"),
        spaceBefore=4,
        spaceAfter=2,
        keepWithNext=True,
    ))
    styles.add(ParagraphStyle(
        name="ResumeBody",
        parent=styles["BodyText"],
        fontName="Helvetica",
        fontSize=9,
        leading=12,
        textColor=colors.HexColor("#263238"),
        spaceAfter=3,
    ))

    def paragraph(text: str, style: str = "ResumeBody") -> Paragraph:
        normalized = unicodedata.normalize("NFKD", str(text or ""))
        ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
        return Paragraph(escape(ascii_text).replace("\n", "<br/>"), styles[style])

    story = [paragraph(resume.full_name, "ResumeName")]
    if resume.title:
        story.append(paragraph(resume.title, "ResumeSubtitle"))

    contact = [part for part in (resume.email, resume.phone, resume.location) if part]
    profile_links = [
        label for url, label in (
            (resume.linkedin_url, "LinkedIn"),
            (resume.github_url, "GitHub"),
            (resume.portfolio_url, "Portfolio"),
        ) if url
    ]
    if profile_links:
        contact.extend(profile_links)
    if contact:
        story.append(paragraph("  |  ".join(contact), "ResumeSubtitle"))
    story.append(HRFlowable(
        width="100%",
        thickness=0.8,
        color=colors.HexColor("#CBD5DF"),
        spaceBefore=3,
        spaceAfter=8,
    ))

    def section(title: str) -> None:
        story.append(KeepTogether([
            paragraph(title, "ResumeSection"),
            HRFlowable(
                width="100%",
                thickness=0.5,
                color=colors.HexColor("#CBD5DF"),
                spaceAfter=5,
            ),
        ]))

    if resume.summary:
        section("Summary")
        story.append(paragraph(resume.summary.strip()))

    if resume.experiences:
        section("Experience")
        for exp in resume.experiences:
            dates = " - ".join(
                value for value in (
                    exp.start_date,
                    exp.end_date or ("Present" if exp.is_current else None),
                ) if value
            )
            details = [value for value in (exp.company, exp.location, dates) if value]
            entry = [paragraph(
                f"{exp.role}  |  {'  |  '.join(details)}",
                "ResumeEntry",
            )]
            if exp.description:
                entry.append(paragraph(exp.description.strip()))
            entry.extend(paragraph(f"- {bullet.strip()}") for bullet in (exp.bullets or []) if bullet.strip())
            story.append(KeepTogether(entry))

    if resume.educations:
        section("Education")
        for edu in resume.educations:
            dates = " - ".join(
                value for value in (
                    edu.start_date,
                    edu.end_date or ("Present" if edu.is_current else None),
                ) if value
            )
            details = [value for value in (edu.institution, edu.location, dates) if value]
            story.append(paragraph(
                f"{edu.degree}  |  {'  |  '.join(details)}",
                "ResumeEntry",
            ))
            for detail in (edu.field_of_study, f"GPA: {edu.gpa}" if edu.gpa else None,
                           f"Honors: {edu.honors_coursework}" if edu.honors_coursework else None):
                if detail:
                    story.append(paragraph(detail))

    if resume.skills:
        section("Skills")
        grouped: dict[str, list[str]] = {}
        for skill in resume.skills:
            grouped.setdefault(skill.category, []).append(skill.name)
        for category, names in grouped.items():
            story.append(paragraph(f"{category}: {', '.join(names)}"))

    if resume.projects:
        section("Projects")
        for project in resume.projects:
            entry = [paragraph(project.title, "ResumeEntry")]
            if project.description:
                entry.append(paragraph(project.description.strip()))
            if project.tech_stack:
                entry.append(paragraph(f"Tech: {', '.join(project.tech_stack)}"))
            entry.extend(paragraph(f"- {bullet.strip()}") for bullet in (project.bullets or []) if bullet.strip())
            story.append(KeepTogether(entry))

    links = [
        f"{label}: {url}"
        for url, label in (
            (resume.linkedin_url, "LinkedIn"),
            (resume.github_url, "GitHub"),
            (resume.portfolio_url, "Portfolio"),
        )
        if url
    ]
    if links:
        section("Links")
        story.extend(paragraph(link) for link in links)

    document.build(story)
    return buffer.getvalue()


def _build_docx_resume_bytes(resume: models.Resume) -> bytes:
    body_lines = _resume_document_lines(resume)
    paragraphs = "".join(
        f"<w:p><w:r><w:t xml:space=\"preserve\">{escape(_sanitize_word_text(line))}</w:t></w:r></w:p>"
        for line in body_lines
    )

    content_types = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
  <Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>
  <Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>
</Types>
"""
    rels = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>
  <Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>
</Relationships>
"""

    document = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:wpc="http://schemas.microsoft.com/office/word/2010/wordprocessingCanvas" xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006" xmlns:o="urn:schemas-microsoft-com:office:office" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" xmlns:m="http://schemas.openxmlformats.org/officeDocument/2006/math" xmlns:v="urn:schemas-microsoft-com:vml" xmlns:wp14="http://schemas.microsoft.com/office/word/2010/wordprocessingDrawing" xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" xmlns:w10="urn:schemas-microsoft-com:office:word" xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml" xmlns:w15="http://schemas.microsoft.com/office/word/2012/wordml" xmlns:wpg="http://schemas.microsoft.com/office/word/2010/wordprocessingGroup" xmlns:wpi="http://schemas.microsoft.com/office/word/2010/wordprocessingInk" xmlns:wne="http://schemas.microsoft.com/office/word/2006/wordml" xmlns:wps="http://schemas.microsoft.com/office/word/2010/wordprocessingShape" mc:Ignorable="w14 w15 wp14">
  <w:body>
    {paragraphs}
    <w:sectPr>
      <w:pgSz w:w="12240" w:h="15840"/>
      <w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440" w:header="720" w:footer="720" w:gutter="0"/>
    </w:sectPr>
  </w:body>
</w:document>
"""

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    core = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" xmlns:dcmitype="http://purl.org/dc/dcmitype/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <dc:title>Resume Export</dc:title>
  <dc:creator>Resume Builder</dc:creator>
  <cp:lastModifiedBy>Resume Builder</cp:lastModifiedBy>
  <dcterms:created xsi:type="dcterms:W3CDTF">{timestamp}</dcterms:created>
  <dcterms:modified xsi:type="dcterms:W3CDTF">{timestamp}</dcterms:modified>
</cp:coreProperties>
"""

    app = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties" xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">
  <Application>Resume Builder</Application>
</Properties>
"""

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", rels)
        archive.writestr("docProps/core.xml", core)
        archive.writestr("docProps/app.xml", app)
        archive.writestr("word/document.xml", document)
    return buffer.getvalue()


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


@app.get("/resumes/{resume_id}/export")
def export_resume_document(
    resume_id: int,
    format: str = Query(default="pdf", pattern="^(pdf|word|docx)$"),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Download the resume as a PDF or editable Word document."""
    resume = _full_resume(db, resume_id, current_user)
    filename = re.sub(r"[^A-Za-z0-9._-]+", "-", (resume.title or resume.full_name or "resume").strip())
    filename = filename.strip("-") or "resume"

    if format == "pdf":
        content = _build_pdf_resume_bytes(resume)
        return Response(
            content=content,
            media_type="application/pdf",
            headers={"Content-Disposition": f'attachment; filename="{filename}.pdf"'},
        )

    content = _build_docx_resume_bytes(resume)
    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f'attachment; filename="{filename}.docx"'},
    )


@app.get("/resumes/{resume_id}/export/pdf")
def export_resume_pdf(
    resume_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Download the resume as a PDF."""
    resume = _full_resume(db, resume_id, current_user)
    filename = re.sub(r"[^A-Za-z0-9._-]+", "-", (resume.title or resume.full_name or "resume").strip())
    filename = filename.strip("-") or "resume"
    return Response(
        content=_build_pdf_resume_bytes(resume),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}.pdf"'},
    )


@app.get("/resumes/{resume_id}/export/word")
def export_resume_word(
    resume_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Download the resume as a Word document."""
    resume = _full_resume(db, resume_id, current_user)
    filename = re.sub(r"[^A-Za-z0-9._-]+", "-", (resume.title or resume.full_name or "resume").strip())
    filename = filename.strip("-") or "resume"
    return Response(
        content=_build_docx_resume_bytes(resume),
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f'attachment; filename="{filename}.docx"'},
    )


@app.get("/resumes/{resume_id}/export/docx")
def export_resume_docx(
    resume_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Alias for Word export."""
    return export_resume_word(resume_id, db, current_user)


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
async def _groq_chat(prompt: str, temperature: float = 0.7) -> str:
    """Send a prompt to Groq and return the raw completion text."""
    client = get_groq_client()
    if not client:
        raise HTTPException(
            status_code=500,
            detail="GROQ_API_KEY is not configured on the server."
        )

    model_name = os.getenv("GROQ_MODEL", "qwen/qwen3.8-27b")
    try:
        max_tokens = max(128, min(int(os.getenv("GROQ_MAX_TOKENS", "1200")), 4000))
    except ValueError:
        max_tokens = 1200
    try:
        completion = client.chat.completions.create(
            messages=[{"role": "user", "content": prompt}],
            model=model_name,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        content = completion.choices[0].message.content or ""
        return content.strip()
    except groq.RateLimitError:
        raise HTTPException(
            status_code=429,
            detail="AI rate limit reached. Please wait a few seconds and try again."
        )
    except groq.APIError:
        logger.exception("AI provider request failed")
        raise HTTPException(
            status_code=502,
            detail="The AI provider is temporarily unavailable. Please try again.",
        )
    except HTTPException:
        raise
    except Exception:
        logger.exception("Unexpected AI provider failure")
        raise HTTPException(
            status_code=502,
            detail="The AI service could not complete this request. Please try again.",
        )


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
async def generate_summary(
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
    ai_text = await _groq_chat(prompt, temperature=0.7)
    return {"summary": ai_text}


@app.post("/ai/rewrite-bullet-xyz", response_model=schemas.AIRewriteBulletResponse)
async def rewrite_bullet_xyz(
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

    raw = await _groq_chat(prompt, temperature=0.8)
    variants = _parse_bullet_variants(raw)
    if not variants:
        raise HTTPException(
            status_code=502,
            detail="The AI did not return any usable bullet variants. Please try again.",
        )
    return {"variants": variants, "formula": XYZ_FORMULA}


@app.post("/ai/tailor-resume", response_model=schemas.AITailorResponse)
async def tailor_resume(
    payload: schemas.AITailorRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Analyze a resume against a target job description for ATS matching and improvements."""
    return await _tailor_resume_analysis(payload, db, current_user)


async def _tailor_resume_analysis(
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

    raw = await _groq_chat(prompt, temperature=0.3)
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
async def analyze_application(
    application_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    application = _owned_application(db, application_id, current_user)
    analysis = await _tailor_resume_analysis(
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
async def suggest_skills(
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

    raw = await _groq_chat(prompt, temperature=0.5)
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
