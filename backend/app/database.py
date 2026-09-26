from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

# check_same_thread=False is needed for SQLite since FastAPI may use the
# connection from a different thread than the one that created it; SQLAlchemy's
# session-per-request pattern still keeps access safe.
DATABASE_URL = "sqlite:////app/data/netbox_manager.db"
engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},
    pool_pre_ping=True,
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
