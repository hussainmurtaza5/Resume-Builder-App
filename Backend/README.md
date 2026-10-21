# Resume Builder API

FastAPI backend for the Resume Builder app. SQLite + SQLAlchemy, JWT auth, Groq-powered AI.

## Run

```bash
pip install -r requirements.txt
uvicorn main:app --reload
```

On startup the app creates any missing tables and runs the lightweight column
migrations in `migrations.py`, so an existing database is upgraded in place.

Env vars (`.env`): `GROQ_API_KEY`, `GROQ_MODEL`, `FRONTEND_ORIGINS`, `SECRET_KEY`.

## Auth

| Method | Path | Body | Returns |
| --- | --- | --- | --- |
| POST | `/register` | `{email, password}` | user |
| POST | `/login` | form `username`/`password` | `{access_token, token_type}` |

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

`duplicate` takes an optional body to override the clone:

```json
{ "title": "Acme - Backend Engineer", "full_name": "Ada Lovelace", "summary": "..." }
```

Resume fields: `title`, `full_name`, `email`, `phone`, `location`, `linkedin_url`,
`github_url`, `portfolio_url`, `summary`.

### Education — `/resumes/{resume_id}/educations`

| Method | Path |
| --- | --- |
| POST | `/resumes/{resume_id}/educations` |
| GET | `/resumes/{resume_id}/educations` |
| PATCH | `/resumes/{resume_id}/educations/{edu_id}` |
| DELETE | `/resumes/{resume_id}/educations/{edu_id}` |

Fields: `institution`, `degree`, `field_of_study`, `location`, `start_date`,
`end_date`, `is_current`, `gpa`, `honors_coursework`, `sort_order`.

### Experience — `/resumes/{resume_id}/experiences`

| Method | Path |
| --- | --- |
| POST | `/resumes/{resume_id}/experiences` |
| GET | `/resumes/{resume_id}/experiences` |
| PATCH | `/resumes/{resume_id}/experiences/{exp_id}` |
| DELETE | `/resumes/{resume_id}/experiences/{exp_id}` |

Fields: `company`, `role`, `years`, `location`, `start_date`, `end_date`,
`is_current`, `description`, `bullets` (list of Google-style bullets), `sort_order`.

### Skills — `/resumes/{resume_id}/skills`

| Method | Path | Notes |
| --- | --- | --- |
| POST | `/resumes/{resume_id}/skills` | one skill (`name`) and/or a batch (`names`) per category |
| GET | `/resumes/{resume_id}/skills` | grouped by category |
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
| PATCH | `/resumes/{resume_id}/projects/{project_id}` |
| DELETE | `/resumes/{resume_id}/projects/{project_id}` |

Fields: `title`, `tech_stack` (list), `description`, `bullets` (list),
`github_url`, `live_url`, `sort_order`.

## AI

| Method | Path | Notes |
| --- | --- | --- |
| POST | `/ai/generate-summary` | `{full_name, experiences[], current_summary}` → `{summary}` |
| POST | `/ai/rewrite-bullet-xyz` | Google "X-Y-Z" bullet rewriter |

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
