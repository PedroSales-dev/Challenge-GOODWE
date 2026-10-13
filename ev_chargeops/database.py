"""
Configuração do banco de dados.

Decisão de desvio Sprint 02: o esquema foi desenhado para PostgreSQL + TimescaleDB
(Sprint 01), mas o protótipo usa SQLite para eliminar dependência de container externo.
A troca de engine é feita apenas neste arquivo — o restante do código não muda.
"""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

DATABASE_URL = "sqlite:///./ev_chargeops.db"

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},
    echo=False,
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def create_tables():
    """Cria todas as tabelas definidas nos models."""
    from ev_chargeops import models  # import tardio para evitar circular
    Base.metadata.create_all(bind=engine)


def drop_and_recreate():
    """Utilitário para reset completo — use apenas em ambiente de desenvolvimento."""
    from ev_chargeops import models
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
