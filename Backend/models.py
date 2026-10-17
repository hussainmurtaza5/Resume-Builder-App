from sqlalchemy import Column, Integer, String, Text, ForeignKey
from sqlalchemy.orm import relationship
from database import Base


class Resume(Base):
    __tablename__ = "resumes"

    id = Column(Integer, primary_key=True, index=True)
    full_name = Column(String(100), nullable=False)
    email = Column(String(255), nullable=False, index=True)
    phone = Column(String(30), nullable=True)
    summary = Column(Text, default="")

    experiences = relationship(
        "Experience",
        back_populates="resume",
        # cascade="all, delete-orphan",
    )


class Experience(Base):
    __tablename__ = "experiences"

    id = Column(Integer, primary_key=True, index=True)
    company = Column(String(150), nullable=False)
    role = Column(String(150), nullable=False)
    years = Column(Integer, nullable=False)

    resume_id = Column(Integer, ForeignKey("resumes.id"), nullable=False)
    resume = relationship("Resume", back_populates="experiences")