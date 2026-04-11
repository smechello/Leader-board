# Leader-board: Ideathon Scoreboard System

![Leader-board Banner](docs/assets/banner-hero.svg)

![Python](https://img.shields.io/badge/Python-3.12+-3776AB?logo=python&logoColor=white)
![Flask](https://img.shields.io/badge/Flask-3.1-black?logo=flask)
![PostgreSQL](https://img.shields.io/badge/PostgreSQL-Database-336791?logo=postgresql&logoColor=white)
![License](https://img.shields.io/badge/License-MIT-success)
![Status](https://img.shields.io/badge/Status-Production%20Ready-0ea5e9)

A production-oriented Flask application for running ideathon or hackathon evaluations with three role-based experiences:

- Admin portal for event operations and configuration
- Judge portal for scoring and review
- Public live scoreboard for real-time rankings

It also includes team portal access, direct-login links, login-request approvals, presentation queue control, configurable scoring rules, and bulk JSON data loading.

This README is intentionally designed as an end-to-end operating manual and project showcase: a single place for setup, architecture, event execution, deployment, and maintenance.

## Table of Contents

- Overview
- Why This Project Stands Out
- Visual Highlights
- Feature Matrix
- Role Capability Matrix (Text)
- Core Features
- Tech Stack
- Architecture
- Architecture Diagrams
- Event Lifecycle
- Scoring Pipeline
- Project Structure
- System Design Principles
- Scoring Model
- Prerequisites
- Environment Variables
- Local Setup
- Running the App
- Quickstart in 5 Minutes
- How to Use (Role-Based)
- Bulk Data Load (Admin)
- Operational Runbook
- Testing
- Quality Gates
- Deployment (Render)
- Production Hardening Checklist
- API and Key Routes
- Security Notes
- Contribution Guide
- Roadmap
- License
- Troubleshooting
- FAQ

## Overview

This project is designed for fast event operations with minimal manual data entry during live sessions. The system supports:

- Team and project management
- Judge account and access control management
- Live team presentations with timer and queue controls
- Flexible weighted scoring configuration
- Real-time public ranking with deterministic tie-break behavior
- Bulk initialization and updates through JSON import

## Why This Project Stands Out

- Event-ready operational depth: this is not just a scoreboard; it is a complete event command center
- Strong role separation: Admin, Judge, Team, and Public flows are clearly defined
- Real-time clarity: live ranking with deterministic tie-break rules
- Practical reliability: health endpoint, DB compatibility checks, and schema recovery logic
- Admin productivity: one-click bulk data loading with preview and guarded destructive mode
- Security-aware defaults: role-based routes, secure password handling, short-lived direct links

## Visual Highlights

### Portal Experience Snapshot

![Portal Overview](docs/assets/portals-overview.svg)

### Bulk Data Load Snapshot

![Data Load Workflow](docs/assets/data-load-flow.svg)

### Deployment Flow Snapshot

![Deployment Flow](docs/assets/deployment-flow.svg)

## Feature Matrix

![Feature Matrix](docs/assets/feature-matrix.svg)

## Role Capability Matrix (Text)

| Capability | Admin | Judge | Team | Public |
|---|---|---|---|---|
| Team and project management | Yes | No | No | No |
| Team-by-team scoring | No | Yes | No | No |
| Presentation queue and timer control | Yes | Read-only timer state | No | Read-only timer state |
| Live scoreboard visibility | Yes | Yes | Yes | Yes |
| Direct login links | Yes (manage) | Yes (consume) | Yes (consume) | No |
| Bulk JSON data import | Yes | No | No | No |
| Login request approval flow | Yes | Yes (request/consume) | No | No |

## Core Features

### Admin Portal

- Dashboard with core metrics:
  - Team count
  - Active judge count
  - Pending judge access requests
  - Total score entries
- Team management:
  - Create, edit, delete teams
  - Team member CRUD
  - Team ordering (drag/reorder)
  - Team portal credential setup
  - Team direct-login link creation/revocation
- Presentation control:
  - Queue view with pending/completed counts
  - Timer actions: start, pause, reset
  - Mark team complete with elapsed time capture
  - Reopen team
  - Reset full presentation queue
- Judge management:
  - Create judge accounts
  - Reset judge password
  - Delete judge accounts
  - Judge direct-login link creation/revocation
  - Approve/reject judge login requests
  - Judge online/offline status monitoring
- System options:
  - Scoring weight and max-score configuration
  - Presentation time limit configuration
  - Theme and process option management
  - Filtered bulk score deletion
- Data management:
  - Download JSON template
  - Preview JSON structure and operations
  - Append import mode
  - Clear-and-load mode with admin-password confirmation
  - Generated credentials preview for judges/team portal when needed
- Kill switch:
  - Wipe database and restore defaults (admin-password protected)

### Judge Portal

- Judge dashboard listing teams and progress status
- Team scoring page:
  - Four category scoring with validation
  - Auto-caps scores to category max
  - Save and Save & Next flows
  - Clear saved scores for current team
- Ongoing presence heartbeat endpoint for live online status
- Current scoring behavior allows re-saving/editing (no lock workflow enforced)

### Public Experience

- Home page
- Login page (admin, judge, and team in unified flow)
- Live scoreboard page with periodic refresh and ranking updates
- Tie-break rule included in payload and UI context

### Team Portal

- Team login via team credentials or direct-login link
- Team profile view:
  - Team and project details
  - Member list
  - Judge-wise score breakdown and remarks

## Tech Stack

- Backend: Flask 3
- ORM: Flask-SQLAlchemy / SQLAlchemy 2
- Auth/session: Flask-Login + secure server-side session logic
- Database: PostgreSQL
- Frontend: Jinja2 templates + Bootstrap 5 + Bootstrap Icons
- Deployment: Gunicorn-compatible (Render-ready)
- Testing: Pytest

## Architecture

![Architecture Overview](docs/assets/architecture-overview.svg)

The app is organized around Flask Blueprints and service-layer modules:

- `public` blueprint: public pages, login, team portal, direct links
- `admin` blueprint: event operations and management
- `judge` blueprint: scoring flows

Services encapsulate business logic:

- Scoring calculations and configuration
- Scoreboard aggregation and ranking
- Presence/heartbeat handling
- Bulk data loading, normalization, validation, and import

## Architecture Diagrams

### Request and Module Flow

```mermaid
flowchart LR
  U[Browser users: Admin, Judge, Team, Public] --> R[Flask routes: public.py, admin.py, judge.py]
  R --> S[Service layer: scoring, presence, data load, judge scoring]
  S --> O[SQLAlchemy ORM Models]
  O --> D[(PostgreSQL)]

  R --> T[Jinja templates and Bootstrap]
  T --> U
```

### Bulk Data Import Lifecycle

```mermaid
sequenceDiagram
  participant A as Admin UI
  participant AR as admin.py routes
  participant DL as data_load_service.py
  participant DB as PostgreSQL

  A->>AR: Upload/Paste JSON + mode
  AR->>DL: parse_json_payload()
  AR->>DL: prepare_load_payload()
  DL-->>AR: normalized payload + preview summary
  A->>AR: Confirm import
  AR->>DL: apply_load_payload()
  DL->>DB: insert/update/truncate as per mode
  DB-->>AR: transaction result
  AR-->>A: import summary + success/failure message
```

### Data Model Relationship Map

```mermaid
erDiagram
  USERS ||--o| JUDGES : owns_profile
  JUDGES ||--o{ SCORES : submits
  TEAMS ||--o{ SCORES : receives
  TEAMS ||--o{ TEAM_MEMBERS : contains
  TEAMS ||--o| PROJECTS : has
  JUDGES ||--o{ JUDGE_DIRECT_LOGIN_LINKS : has
  TEAMS ||--o{ TEAM_DIRECT_LOGIN_LINKS : has
  JUDGES ||--o{ JUDGE_LOGIN_REQUESTS : requests
```

### Render Deployment Topology

```mermaid
flowchart LR
  Repo[Git repository] --> Build[Render build stage]
  Build --> Runtime[Gunicorn runtime app:app]
  Runtime --> DB[(PostgreSQL database)]
  Runtime --> Health[Health endpoint]
```

## Event Lifecycle

![Event Lifecycle](docs/assets/event-lifecycle.svg)

## Scoring Pipeline

![Scoring Pipeline](docs/assets/scoring-pipeline.svg)

## Project Structure

```text
Leader-board/
├── app.py
├── config.py
├── requirements.txt
├── schema.sql
├── setup_database.py
├── models/
│   ├── user.py
│   ├── team.py
│   ├── score.py
│   ├── scoring.py
│   ├── options.py
│   ├── auth_access.py
│   ├── presence.py
│   └── audit.py
├── routes/
│   ├── public.py
│   ├── admin.py
│   └── judge.py
├── services/
│   ├── scoring_service.py
│   ├── scoring_config_service.py
│   ├── judge_scoring_service.py
│   ├── presence_service.py
│   └── data_load_service.py
├── utils/
│   ├── auth.py
│   └── team_auth.py
├── templates/
│   ├── base.html
│   ├── admin/
│   ├── judge/
│   └── public/
├── static/
│   └── css/
└── tests/
```

## System Design Principles

- Speed of operation over feature bloat during live event windows
- Safe defaults for destructive operations
- Explicit role ownership and route protection
- Separation of concerns through routes, services, models, and utils
- Configuration through environment variables only
- Database-first reliability for event continuity

## Scoring Model

The system uses four categories:

- Innovation and Originality
- Technical Implementation
- Business Value and Impact
- Presentation and Clarity

Default weights are:

- Innovation and Originality: 30%
- Technical Implementation: 30%
- Business Value and Impact: 25%
- Presentation and Clarity: 15%

Weighted score formula:

$$
\text{weighted score} = \left(\frac{\text{raw score}}{\text{max score}}\right) \times \text{weight percent}
$$

The total team score shown on scoreboard is the sum of weighted entries from all submitted judge-category rows.

Tie-break rule:

- Higher Business Value and Impact weighted total first
- Then earliest score submission timestamp

## Prerequisites

- Python 3.11+ (3.12 recommended)
- PostgreSQL database
- pip

## Environment Variables

Create a `.env` file in `Leader-board/`.

Minimum required variables:

- `DATABASE_URL`
- `ADMIN_USERNAME`
- One of:
  - `ADMIN_PASSWORD`
  - `ADMIN_PASSWORD_HASH`

Common variables:

- `SECRET_KEY`
- `LOG_LEVEL` (default: `INFO`)
- `SESSION_COOKIE_SECURE` (`true` in production, `false` locally)

Example:

```env
SECRET_KEY=change-me-in-production
DATABASE_URL=postgresql://username:password@host:5432/database_name
ADMIN_USERNAME=admin
ADMIN_PASSWORD=change-me
# ADMIN_PASSWORD_HASH=pbkdf2:sha256:...
LOG_LEVEL=INFO
SESSION_COOKIE_SECURE=false
```

## Local Setup

### 1) Clone and enter project

```bash
git clone <your-repo-url>
cd Leader-board
```

### 2) Create virtual environment

Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

macOS/Linux:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 3) Install dependencies

```bash
pip install -r requirements.txt
```

### 4) Configure environment

- Copy `.env.example` to `.env`
- Fill required values

### 5) Initialize database

Option A (recommended first run): interactive setup script

```bash
python setup_database.py
```

Option B: run app directly (it performs compatibility checks and can auto-recover schema from `schema.sql` when needed)

```bash
python app.py
```

## Running the App

Development server:

```bash
python app.py
```

App defaults to Flask development mode when executed directly.

Health check endpoint:

- `GET /health`

## Quickstart in 5 Minutes

```bash
# 1) create and activate env
python -m venv .venv

# Windows PowerShell
.\.venv\Scripts\Activate.ps1

# 2) install dependencies
pip install -r requirements.txt

# 3) configure environment file
copy .env.example .env

# 4) initialize database (recommended)
python setup_database.py

# 5) run app
python app.py
```

## How to Use (Role-Based)

### Admin Workflow

1. Log in using admin credentials
2. Configure base options:
    - Themes
    - Processes
    - Scoring rules
    - Presentation time limit
3. Add teams and project details
4. Add members for each team
5. Create judge accounts
6. (Optional) issue direct-login links for judges/teams
7. Use presentation control during live sessions
8. Monitor pending judge access requests and approve/reject quickly
9. Watch live scoreboard updates

### Judge Workflow

1. Log in with username/password or approved access flow
2. Open team scoring page from judge dashboard
3. Enter all category scores and remarks
4. Save or Save & Next
5. Clear and rescore if needed

### Team Workflow

1. Log in with team portal ID/password or direct link
2. Review members, project details, and judge-wise score visibility

### Public Workflow

1. Open `/scoreboard`
2. Observe live rank changes and tie-break behavior

## Bulk Data Load (Admin)

Data Load supports end-to-end initialization from one JSON payload.

### Modes

- Append mode:
  - Adds missing items
  - Updates matching teams/judges
  - Preserves non-targeted records
- Clear + Load mode:
  - Truncates tables and rebuilds from JSON
  - Requires current admin password

### Input Methods

- Upload `.json` file
- Paste JSON text

### Safe Process

1. Open Data Load from Options page
2. Download template JSON
3. Fill payload
4. Click Preview Structure
5. Verify operations summary and generated credentials
6. Execute import in desired mode

### Expected JSON Sections

- `processes`
- `themes`
- `presentation_settings`
- `scoring_rules`
- `teams`
- `judges`

Minimal example:

```json
{
  "processes": ["General"],
  "themes": ["AI"],
  "teams": [
    {
      "team_name": "Team Alpha",
      "process": "General",
      "theme": "AI",
      "project": {
        "project_title": "Intelligent Ops Assistant",
        "problem_statement": "Operations are too manual",
        "project_summary": "Automates repetitive workflows"
      },
      "members": [
        {
          "full_name": "Member One",
          "email": "member.one@example.com"
        }
      ]
    }
  ],
  "judges": [
    {
      "display_name": "Judge One",
      "username": "judge_one",
      "password": "StrongPass123"
    }
  ]
}
```

## Operational Runbook

### Before Event

- Validate `.env`
- Verify `/health` returns DB connected
- Confirm scoring weights sum to 100%
- Confirm presentation time limit
- Verify judge accounts and test one login
- Verify team portal credentials if enabled
- Seed all teams/members/projects

### During Event

- Use presentation queue and timer controls
- Monitor judge status and pending requests
- Approve requests with minimal delay
- Keep options changes controlled (especially scoring)

### After Event

- Export/store final results from scoreboard and database
- Rotate any temporary credentials
- Revoke remaining direct-login links
- Use append import for incremental updates; reserve clear-load/kill-switch for controlled resets

## Testing

Run all tests:

```bash
pytest
```

Run one integration file:

```bash
pytest tests/test_routes_integration.py
```

Notes:

- Tests require environment variables (especially `DATABASE_URL`)
- Destructive kill-switch test is disabled by default and only runs with:
  - `RUN_DESTRUCTIVE_TESTS=1`

## Quality Gates

- Route integration tests for admin, judge, and public workflows
- Scoring unit checks for category and weighted calculations
- Template and Python diagnostics for changed files
- Focused regression checks for recently changed areas
- Optional full suite execution before release/deploy

## Deployment (Render)

Typical Render setup:

- Environment: Python
- Build command:

```bash
pip install -r requirements.txt
```

- Start command:

```bash
gunicorn app:app
```

Required environment variables in Render:

- `DATABASE_URL`
- `ADMIN_USERNAME`
- `ADMIN_PASSWORD` or `ADMIN_PASSWORD_HASH`
- `SECRET_KEY`
- `SESSION_COOKIE_SECURE=true`

Recommended:

- Use managed PostgreSQL with SSL
- Use hashed admin password (`ADMIN_PASSWORD_HASH`) instead of plaintext admin password

## Production Hardening Checklist

- Set strong and unique `SECRET_KEY`
- Use `ADMIN_PASSWORD_HASH` instead of plaintext admin password
- Set `SESSION_COOKIE_SECURE=true`
- Restrict direct-login lifespan (short expiry windows)
- Back up database before clear-load or kill-switch operations
- Keep scoring configuration changes controlled and documented
- Validate `/health` status after each deployment
- Run test suite against target database environment

## API and Key Routes

Public:

- `GET /`
- `GET /scoreboard`
- `GET /api/scoreboard`
- `GET|POST /login`
- `GET /health`

Admin:

- `GET /admin/dashboard`
- `GET /admin/teams`
- `GET /admin/judges`
- `GET /admin/presentation`
- `GET /admin/options`
- `GET|POST /admin/load-data...`

Judge:

- `GET /judge/dashboard`
- `GET|POST /judge/teams/<team_id>/score`
- `POST /judge/presence/heartbeat`

## Security Notes

- Set a strong `SECRET_KEY`
- Prefer `ADMIN_PASSWORD_HASH` over plaintext admin password
- Keep `SESSION_COOKIE_SECURE=true` in production
- Keep direct-login links short-lived
- Restrict destructive actions (clear-load and kill-switch) to authorized admin only
- Backup database before destructive operations

## Contribution Guide

1. Create a feature branch from your default branch.
2. Implement changes with focused commits.
3. Run tests locally (`pytest`).
4. Verify critical pages: admin dashboard, options, judges, team scoring, scoreboard.
5. Open a PR with change summary, risk notes, and test evidence.

Recommended contribution areas:

- Additional analytics and insights for judges and admins
- Better export formats for results
- Expanded automated test coverage for edge-case event flows
- Optional API auth wrappers for future external integrations

## Roadmap

- [ ] Export center for CSV/PDF score reports
- [ ] Role-based analytics cards and trend charts
- [ ] Optional multi-event workspace partitioning
- [ ] Extended audit views with filters and timeline
- [ ] API token-based access for external dashboards
- [ ] Backup/restore utility workflow from admin UI

## License

This project is licensed under the MIT License.

See [LICENSE](LICENSE) for full text.

## Troubleshooting

### App fails at startup with missing env variables

- Ensure `.env` contains all required values
- Required set includes DB URL, admin username, and one admin password strategy

### Database schema mismatch errors

- App attempts automatic recovery from `schema.sql`
- You can also run `python setup_database.py` explicitly

### Scoreboard not updating as expected

- Verify score rows exist for teams
- Confirm judges are saving all categories
- Confirm scoring settings are valid and totals sum to 100

### Judge appears offline while active

- Presence status uses heartbeat TTL logic
- Temporary offline display can happen if heartbeat pauses beyond TTL

## FAQ

### Can judges edit scores after saving?

Yes. The current scoring flow supports re-saving and updates.

### Can we reset everything quickly?

Yes, with admin protections:

- Clear + Load mode in Data Load
- Kill switch route (wipes and restores defaults)

### Can we preload all event data before day one?

Yes. Use Data Load template + preview + append import.
