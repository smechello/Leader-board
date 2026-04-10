from flask_sqlalchemy import SQLAlchemy


db = SQLAlchemy()


from .audit import AuditLog  # noqa: E402,F401
from .options import ProcessOption, ThemeOption  # noqa: E402,F401
from .score import Score  # noqa: E402,F401
from .team import Project, Team, TeamMember  # noqa: E402,F401
from .user import Judge, User  # noqa: E402,F401
