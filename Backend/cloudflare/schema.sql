CREATE TABLE IF NOT EXISTS users (
	id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT, 
	email VARCHAR(255) NOT NULL, 
	hashed_password VARCHAR(255) NOT NULL, 
	created_at DATETIME
);

CREATE TABLE IF NOT EXISTS resumes (
	id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT, 
	title VARCHAR(150), 
	full_name VARCHAR(100) NOT NULL, 
	email VARCHAR(255) NOT NULL, 
	phone VARCHAR(30), 
	location VARCHAR(120), 
	linkedin_url VARCHAR(255), 
	github_url VARCHAR(255), 
	portfolio_url VARCHAR(255), 
	summary TEXT, 
	user_id INTEGER NOT NULL, 
	FOREIGN KEY(user_id) REFERENCES users (id)
);

CREATE TABLE IF NOT EXISTS applications (
	id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT, 
	company VARCHAR(150) NOT NULL, 
	role VARCHAR(150) NOT NULL, 
	job_url VARCHAR(2048), 
	job_description TEXT NOT NULL, 
	status VARCHAR(20) NOT NULL, 
	notes TEXT NOT NULL, 
	match_score INTEGER, 
	analysis JSON, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	analyzed_at DATETIME, 
	user_id INTEGER NOT NULL, 
	source_resume_id INTEGER, 
	tailored_resume_id INTEGER NOT NULL, 
	FOREIGN KEY(user_id) REFERENCES users (id), 
	FOREIGN KEY(source_resume_id) REFERENCES resumes (id) ON DELETE SET NULL, 
	UNIQUE (tailored_resume_id), 
	FOREIGN KEY(tailored_resume_id) REFERENCES resumes (id)
);

CREATE TABLE IF NOT EXISTS educations (
	id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT, 
	institution VARCHAR(150) NOT NULL, 
	degree VARCHAR(100) NOT NULL, 
	field_of_study VARCHAR(120), 
	location VARCHAR(120), 
	start_date VARCHAR(20), 
	end_date VARCHAR(20), 
	is_current BOOLEAN NOT NULL, 
	gpa VARCHAR(20), 
	honors_coursework TEXT, 
	sort_order INTEGER NOT NULL, 
	resume_id INTEGER NOT NULL, 
	FOREIGN KEY(resume_id) REFERENCES resumes (id)
);

CREATE TABLE IF NOT EXISTS experiences (
	id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT, 
	company VARCHAR(150) NOT NULL, 
	role VARCHAR(150) NOT NULL, 
	years INTEGER NOT NULL, 
	location VARCHAR(120), 
	start_date VARCHAR(20), 
	end_date VARCHAR(20), 
	is_current BOOLEAN NOT NULL, 
	description TEXT, 
	bullets JSON NOT NULL, 
	sort_order INTEGER NOT NULL, 
	resume_id INTEGER NOT NULL, 
	FOREIGN KEY(resume_id) REFERENCES resumes (id)
);

CREATE TABLE IF NOT EXISTS projects (
	id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT, 
	title VARCHAR(150) NOT NULL, 
	tech_stack JSON NOT NULL, 
	description TEXT, 
	bullets JSON NOT NULL, 
	github_url VARCHAR(255), 
	live_url VARCHAR(255), 
	sort_order INTEGER NOT NULL, 
	resume_id INTEGER NOT NULL, 
	FOREIGN KEY(resume_id) REFERENCES resumes (id)
);

CREATE TABLE IF NOT EXISTS skills (
	id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT, 
	category VARCHAR(60) NOT NULL, 
	name VARCHAR(60) NOT NULL, 
	sort_order INTEGER NOT NULL, 
	resume_id INTEGER NOT NULL, 
	CONSTRAINT uq_resume_category_skill UNIQUE (resume_id, category, name), 
	FOREIGN KEY(resume_id) REFERENCES resumes (id)
);

CREATE UNIQUE INDEX IF NOT EXISTS ix_users_email ON users (email);
CREATE INDEX IF NOT EXISTS ix_resumes_email ON resumes (email);
CREATE INDEX IF NOT EXISTS ix_applications_user_id ON applications (user_id);
CREATE INDEX IF NOT EXISTS ix_applications_status ON applications (status);
CREATE INDEX IF NOT EXISTS ix_skills_category ON skills (category);
