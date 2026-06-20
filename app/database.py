from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

SQLALCHEMY_DATABASE_URL = "sqlite:///./gridsync.db"

engine = create_engine(
    SQLALCHEMY_DATABASE_URL,
    connect_args={"check_same_thread": False},
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    import app.models  # noqa: F401  ensure models are registered

    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)


def create_tables() -> None:
    import app.models  # noqa: F401  ensure models are registered

    Base.metadata.create_all(bind=engine)
