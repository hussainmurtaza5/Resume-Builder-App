from fastapi import FastAPI, Depends, HTTPException
from sqlalchemy.orm import Session

import models
import schemas
from database import engine, SessionLocal

models.Base.metadata.create_all(bind=engine)

app = FastAPI(title="Resume Builder API")


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@app.get("/")
def read_root():
    return {"message": "Backend is alive"}


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/resumes", response_model=schemas.ResumeResponse, status_code=201)
def create_resume(payload: schemas.ResumeCreate, db: Session = Depends(get_db)):
    resume = models.Resume(
        full_name=payload.full_name,
        email=payload.email,
        phone=payload.phone,
        summary=payload.summary,
    )
    db.add(resume)
    db.flush()   # assigns resume.id without committing

    for exp in payload.experiences:
        db.add(models.Experience(
            company=exp.company,
            role=exp.role,
            years=exp.years,
            resume_id=resume.id,
        ))

    db.commit()
    db.refresh(resume)   # reload with ids and relationships populated
    return resume


@app.get("/resumes", response_model=list[schemas.ResumeResponse])
def list_resumes(db: Session = Depends(get_db)):
    return db.query(models.Resume).all()


@app.get("/resumes/{resume_id}", response_model=schemas.ResumeResponse)
def get_resume(resume_id: int, db: Session = Depends(get_db)):
    resume = db.query(models.Resume).filter(models.Resume.id == resume_id).first()
    if resume is None:
        raise HTTPException(status_code=404, detail="Resume not found")
    return resume
@app.patch("/resumes/{resume_id}", response_model=schemas.ResumeResponse)
def update_resume(resume_id: int, payload: schemas.ResumeUpdate, db: Session = Depends(get_db)):
    resume = db.query(models.Resume).filter(models.Resume.id == resume_id).first()
    if resume is None:
        raise HTTPException(status_code=404, detail="Resume not found")

    update_data = payload.model_dump(exclude_unset=True)  # only fields the client sent
    for field, value in update_data.items():
        setattr(resume, field, value)

    db.commit()
    db.refresh(resume)
    return resume

@app.delete("/resumes/{resume_id}", status_code=204)
def delete_resume(resume_id: int, db: Session = Depends(get_db)):
    resume = db.query(models.Resume).filter(models.Resume.id == resume_id).first()
    if resume is None:
        raise HTTPException(status_code=404, detail="Resume not found")
    db.delete(resume)
    db.commit()
    return