from fastapi import FastAPI, Depends, HTTPException
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from sqlalchemy import func
from sqlalchemy.orm import Session
from jose import JWTError, jwt

import models
import schemas
from database import engine, SessionLocal
from auth import hash_password, verify_password, create_access_token, SECRET_KEY, ALGORITHM
from fastapi.middleware.cors import CORSMiddleware
import os

models.Base.metadata.create_all(bind=engine)

app = FastAPI(title="Resume Builder API")

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


@app.get("/")
def read_root():
    return {"message": "Backend is alive"}


@app.get("/health")
def health():
    return {"status": "ok"}


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


@app.post("/resumes", response_model=schemas.ResumeResponse, status_code=201)
def create_resume(
    payload: schemas.ResumeCreate, 
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    resume = models.Resume(
        full_name=payload.full_name,
        email=payload.email,
        phone=payload.phone,
        summary=payload.summary,
        user_id=current_user.id
    )
    db.add(resume)
    db.flush()  

    for exp in payload.experiences:
        db.add(models.Experience(
            company=exp.company,
            role=exp.role,
            years=exp.years,
            resume_id=resume.id,
        ))

    db.commit()
    db.refresh(resume)
    return resume


@app.get("/resumes", response_model=list[schemas.ResumeResponse])
def list_resumes(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    return db.query(models.Resume).filter(models.Resume.user_id == current_user.id).all()


@app.get("/resumes/{resume_id}", response_model=schemas.ResumeResponse)
def get_resume(
    resume_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    resume = db.query(models.Resume).filter(
        models.Resume.id == resume_id, 
        models.Resume.user_id == current_user.id
    ).first()
    if resume is None:
        raise HTTPException(status_code=404, detail="Resume not found")
    return resume


@app.patch("/resumes/{resume_id}", response_model=schemas.ResumeResponse)
def update_resume(
    resume_id: int,
    payload: schemas.ResumeUpdate, 
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    resume = db.query(models.Resume).filter(
        models.Resume.id == resume_id,
        models.Resume.user_id == current_user.id
    ).first()
    if resume is None:
        raise HTTPException(status_code=404, detail="Resume not found")

    update_data = payload.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(resume, field, value)

    db.commit()
    db.refresh(resume)
    return resume


@app.delete("/resumes/{resume_id}", status_code=204)
def delete_resume(
    resume_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    resume = db.query(models.Resume).filter(
        models.Resume.id == resume_id,
        models.Resume.user_id == current_user.id,
    ).first()
    if resume is None:
        raise HTTPException(status_code=404, detail="Resume not found")
    db.delete(resume)
    db.commit()
    return
