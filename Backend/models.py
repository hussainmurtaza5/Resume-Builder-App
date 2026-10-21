from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.ext.mutable import MutableList
from sqlalchemy.orm import relationship
from sqlalchemy.types import JSON
from database import Base
from datetime import datetime, timezone


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String(255), unique=True, nullable=False, index=True)
    hashed_password = Column(String(255), nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    resumes = relationship("Resume", back_populates="user")


class Resume(Base):
    __tablename__ = "resumes"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String(150), nullable=True)
    full_name = Column(String(100), nullable=False)
    email = Column(String(255), nullable=False, index=True)
    phone = Column(String(30), nullable=True)
    location = Column(String(120), nullable=True)
    linkedin_url = Column(String(255), nullable=True)
    github_url = Column(String(255), nullable=True)
    portfolio_url = Column(String(255), nullable=True)
    summary = Column(Text, default="")

    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    user = relationship("User", back_populates="resumes")

    experiences = relationship(
        "Experience",
        back_populates="resume",
        cascade="all, delete-orphan",
        order_by="Experience.sort_order, Experience.id",
    )
    educations = relationship(
        "Education",
        back_populates="resume",
        cascade="all, delete-orphan",
        order_by="Education.sort_order, Education.id",
    )
    skills = relationship(
        "Skill",
        back_populates="resume",
        cascade="all, delete-orphan",
        order_by="Skill.sort_order, Skill.id",
    )
    projects = relationship(
        "Project",
        back_populates="resume",
        cascade="all, delete-orphan",
        order_by="Project.sort_order, Project.id",
    )


class Experience(Base):
    __tablename__ = "experiences"

    id = Column(Integer, primary_key=True, index=True)
    company = Column(String(150), nullable=False)
    role = Column(String(150), nullable=False)
    years = Column(Integer, nullable=False, default=0)
    location = Column(String(120), nullable=True)
    start_date = Column(String(20), nullable=True)
    end_date = Column(String(20), nullable=True)
    is_current = Column(Boolean, nullable=False, default=False)
    description = Column(Text, default="")
    bullets = Column(MutableList.as_mutable(JSON), nullable=False, default=list)
    sort_order = Column(Integer, nullable=False, default=0)

    resume_id = Column(Integer, ForeignKey("resumes.id"), nullable=False)
    resume = relationship("Resume", back_populates="experiences")


class Education(Base):
    __tablename__ = "educations"

    id = Column(Integer, primary_key=True, index=True)
    institution = Column(String(150), nullable=False)
    degree = Column(String(100), nullable=False)
    field_of_study = Column(String(120), nullable=True)
    location = Column(String(120), nullable=True)
    start_date = Column(String(20), nullable=True)
    end_date = Column(String(20), nullable=True)
    is_current = Column(Boolean, nullable=False, default=False)
    gpa = Column(String(20), nullable=True)
    honors_coursework = Column(Text, default="")
    sort_order = Column(Integer, nullable=False, default=0)

    resume_id = Column(Integer, ForeignKey("resumes.id"), nullable=False)
    resume = relationship("Resume", back_populates="educations")


class Skill(Base):
    __tablename__ = "skills"
    __table_args__ = (
        UniqueConstraint("resume_id", "category", "name", name="uq_resume_category_skill"),
    )

    id = Column(Integer, primary_key=True, index=True)
    category = Column(String(60), nullable=False, index=True)
    name = Column(String(60), nullable=False)
    sort_order = Column(Integer, nullable=False, default=0)

    resume_id = Column(Integer, ForeignKey("resumes.id"), nullable=False)
    resume = relationship("Resume", back_populates="skills")


class Project(Base):
    __tablename__ = "projects"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String(150), nullable=False)
    tech_stack = Column(MutableList.as_mutable(JSON), nullable=False, default=list)
    description = Column(Text, default="")
    bullets = Column(MutableList.as_mutable(JSON), nullable=False, default=list)
    github_url = Column(String(255), nullable=True)
    live_url = Column(String(255), nullable=True)
    sort_order = Column(Integer, nullable=False, default=0)

    resume_id = Column(Integer, ForeignKey("resumes.id"), nullable=False)
    resume = relationship("Resume", back_populates="projects")
