from fastapi import FastAPI

app = FastAPI(title="Resume Builder API")


@app.get("/")
def read_root():
    return {"message": "Backend is alive"}


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/hello/{name}")
def hello(name: str):
    return {"message": f"Hello {name}"}