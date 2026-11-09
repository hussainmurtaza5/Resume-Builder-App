# Resume Builder API

FastAPI backend for the Resume Builder app. SQLite + SQLAlchemy, JWT auth, Groq-powered AI.

## Run

```bash
pip install -r requirements.txt
uvicorn main:app --reload
```

On startup the app creates any missing tables and runs the lightweight column
migrations in `migrations.py`, so an existing database is upgraded in place.

Env vars (`.env`): `GROQ_API_KEY`, `GROQ_MODEL`, `GROQ_MAX_TOKENS`,
`FRONTEND_ORIGINS`, `SECRET_KEY`, and `APP_ENV`. Copy `.env.example` as a
starting point. Keep all AI credentials and signing keys in the backend/Vercel
environment only; never add them to frontend `VITE_*` variables or commit them.
Production requires a strong `SECRET_KEY`, exact `FRONTEND_ORIGINS` values, and
`APP_ENV=production`.

## Auth

| Method | Path | Body | Returns |
| --- | --- | --- | --- |
| POST | `/register` | `{email, password}` | user |
| POST | `/login` | form `username`/`password` | `{access_token, token_type}` |
| GET | `/users/me` | — | current authenticated user |
| GET | `/me` | — | alias for `/users/me` |

All other endpoints need `Authorization: Bearer <access_token>`.

## Resumes

| Method | Path | Notes |
| --- | --- | --- |
| POST | `/resumes` | accepts nested `experiences`, `educations`, `skills`, `projects` |
| GET | `/resumes` | list of the caller's resumes |
| GET | `/resumes/{resume_id}` | full resume, all sections eagerly loaded |
| PATCH | `/resumes/{resume_id}` | partial update of the header fields |
| DELETE | `/resumes/{resume_id}` | deletes the resume and all sections |
| POST | `/resumes/{resume_id}/duplicate` | clone a resume for a specific job application |
| PATCH | `/resumes/{resume_id}/reorder` | batch reorder items in a section (`{section, ids}`) |
| GET | `/resumes/{resume_id}/export/json` | export standard JSON Resume format (jsonresume.org) |
| GET | `/resumes/{resume_id}/export/pdf` | download the resume as a PDF |
| GET | `/resumes/{resume_id}/export/word` | download the resume as a DOCX file |
| GET | `/resumes/{resume_id}/export?format=pdf` | generic download route for PDF or Word (`pdf` / `word`) |

`duplicate` takes an optional body to override the clone:

```json
{ "title": "Acme - Backend Engineer", "full_name": "Ada Lovelace", "summary": "..." }
```

Resume fields: `title`, `full_name`, `email`, `phone`, `location`, `linkedin_url`,
`github_url`, `portfolio_url`, `summary`.

## Job applications

| Method | Path | Notes |
| --- | --- | --- |
| POST | `/applications` | create an application and an independent copy of a resume |
| GET | `/applications` | list applications; supports `status`, `limit` (1–100), and `offset` |
| GET | `/applications/{application_id}` | details, saved job description, analysis, and tailored resume |
| PATCH | `/applications/{application_id}` | update job details, notes, or status |
| POST | `/applications/{application_id}/analyze` | analyze the saved tailored resume and persist the result |
| DELETE | `/applications/{application_id}` | delete the application and its tailored resume copy |

Create an application with a base resume ID, employer, role, and job description:

```json
{
  "resume_id": 1,
  "company": "Acme",
  "role": "Backend Engineer",
  "job_url": "https://example.com/jobs/123",
  "job_description": "Build and maintain reliable backend services...",
  "notes": "Referred by a former colleague"
}
```

Applications start with the `wishlist` status. Supported statuses are
`wishlist`, `applied`, `interviewing`, `offer`, `rejected`, and `withdrawn`.
The generated resume copy is independent of the source and can be edited with
the normal resume endpoints. Analysis is an explicit action; provider errors
leave the application intact and do not save a partial report. Deleting a
source resume preserves the application and its tailored copy, while deleting
an application's saved resume is only possible by deleting that application.

### Education — `/resumes/{resume_id}/educations`

| Method | Path |
| --- | --- |
| POST | `/resumes/{resume_id}/educations` |
| GET | `/resumes/{resume_id}/educations` |
| GET | `/resumes/{resume_id}/educations/{edu_id}` |
| PATCH | `/resumes/{resume_id}/educations/{edu_id}` |
| DELETE | `/resumes/{resume_id}/educations/{edu_id}` |

Fields: `institution`, `degree`, `field_of_study`, `location`, `start_date`,
`end_date`, `is_current`, `gpa`, `honors_coursework`, `sort_order`.

### Experience — `/resumes/{resume_id}/experiences`

| Method | Path |
| --- | --- |
| POST | `/resumes/{resume_id}/experiences` |
| GET | `/resumes/{resume_id}/experiences` |
| GET | `/resumes/{resume_id}/experiences/{exp_id}` |
| PATCH | `/resumes/{resume_id}/experiences/{exp_id}` |
| DELETE | `/resumes/{resume_id}/experiences/{exp_id}` |

Fields: `company`, `role`, `years`, `location`, `start_date`, `end_date`,
`is_current`, `description`, `bullets` (list of Google-style bullets), `sort_order`.

### Skills — `/resumes/{resume_id}/skills`

| Method | Path | Notes |
| --- | --- | --- |
| POST | `/resumes/{resume_id}/skills` | one skill (`name`) and/or a batch (`names`) per category |
| GET | `/resumes/{resume_id}/skills` | grouped by category |
| GET | `/resumes/{resume_id}/skills/{skill_id}` | single skill |
| PATCH | `/resumes/{resume_id}/skills/{skill_id}` | rename / recategorise |
| DELETE | `/resumes/{resume_id}/skills/{skill_id}` | remove a skill |

```jsonc
// single
{ "category": "Languages", "name": "Python" }
// batch
{ "category": "Frameworks", "names": ["FastAPI", "React"] }
```

Duplicates (same category + name) are ignored. `GET` returns:

```json
[{ "category": "Languages", "skills": [{ "id": 1, "name": "Python", "sort_order": 0 }] }]
```

### Projects — `/resumes/{resume_id}/projects`

| Method | Path |
| --- | --- |
| POST | `/resumes/{resume_id}/projects` |
| GET | `/resumes/{resume_id}/projects` |
| GET | `/resumes/{resume_id}/projects/{project_id}` |
| PATCH | `/resumes/{resume_id}/projects/{project_id}` |
| DELETE | `/resumes/{resume_id}/projects/{project_id}` |

Fields: `title`, `tech_stack` (list), `description`, `bullets` (list),
`github_url`, `live_url`, `sort_order`.

## AI

| Method | Path | Notes |
| --- | --- | --- |
| POST | `/ai/generate-summary` | `{full_name, experiences[], current_summary}` → `{summary}` |
| POST | `/ai/rewrite-bullet-xyz` | Google "X-Y-Z" bullet rewriter |
| POST | `/ai/tailor-resume` | ATS match score, keyword comparison & suggestions for a job description |
| POST | `/ai/suggest-skills` | industry-standard skills for a target job title |

`/ai/rewrite-bullet-xyz` takes a rough duty and returns 2–3 high-impact bullets
following *"Accomplished [X], as measured by [Y], by doing [Z]"*:

```jsonc
// request
{ "bullet": "Helped improve database queries", "role": "Backend Engineer", "company": "Acme" }
// response
{
  "variants": [
    "Reduced database query latency by [40%] by implementing composite indexes and query plan analysis."
  ],
  "formula": "Accomplished [X], as measured by [Y], by doing [Z]"
}
```

Missing metrics are returned as bracketed placeholders (`[25%]`) instead of
invented numbers.

## Notes

- Every endpoint is scoped to the authenticated user — a resume owned by someone
  else returns `404`.
- Section rows are scoped to their resume, so `edu_id`/`exp_id`/`skill_id`/
  `project_id` from another resume cannot be modified.
- `sort_order` controls the order sections are returned in (ascending).
- Dates are free-form strings (`"Jan 2021"`) so the frontend controls formatting.

## Cloudflare Workers

The Worker project is in `cloudflare/` so the existing `requirements.txt` remains
available for local and conventional Python deployments. It uses Python Workers,
FastAPI, SQLAlchemy, and a D1 binding.
The shared backend modules live in `cloudflare/src/` so Pywrangler only scans the
application source instead of the backend's local virtual environments;
`main.py` preserves the usual `uvicorn main:app` entrypoint.

1. Install Node.js and `uv` 0.13.4 or newer, then change to `Backend/cloudflare`.
2. Authenticate with Cloudflare: `uv run pywrangler login`.
3. Create a fresh D1 database: `uv run pywrangler d1 create resume-builder-db`.
   Copy the returned database ID into `wrangler.jsonc`.
4. Replace `FRONTEND_ORIGINS` in `wrangler.jsonc` with the exact deployed
   frontend origin.
5. Add production secrets without committing them:

   ```sh
   uv run pywrangler secret put SECRET_KEY
   uv run pywrangler secret put GROQ_API_KEY
   ```

   Use a strong random `SECRET_KEY`. `GROQ_API_KEY` is required only for the AI
   endpoints.
6. Test locally with `uv run pywrangler dev`, then deploy with
   `uv run pywrangler deploy`.

The Worker creates its schema in D1 when it starts. This setup uses a new,
empty database; it does not import data from `resume_builder.db`. D1 data is
persistent, unlike a SQLite file in a Worker bundle.
