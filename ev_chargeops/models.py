"""
Modelos ORM — mapeados ao esquema ER definido na Sprint 01.

Entidades: Unidade, Usuario, CartaoRFID, PontoRecarga,
           SessaoRecarga, EventoTelemetria, Anomalia, Fatura, ItemFatura.
"""
import uuid
from datetime import datetime
from sqlalchemy import (
    Column, String, Float, DateTime, ForeignKey, Integer, Text
)
from sqlalchemy.orm import relationship
from ev_chargeops.database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


class Unidade(Base):
    __tablename__ = "unidades"

    id = Column(String(36), primary_key=True, default=_uuid)
    identificacao = Column(String(50), nullable=False, unique=True)  # ex: "Ap. 101"
    fracao_ideal = Column(Float, nullable=False)                      # 0.0 – 1.0, soma == 1

    usuarios = relationship("Usuario", back_populates="unidade", cascade="all, delete-orphan")
    faturas = relationship("Fatura", back_populates="unidade", cascade="all, delete-orphan")


class Usuario(Base):
    __tablename__ = "usuarios"

    id = Column(String(36), primary_key=True, default=_uuid)
    unidade_id = Column(String(36), ForeignKey("unidades.id"), nullable=False)
    nome = Column(String(100), nullable=False)
    papel = Column(String(20), default="morador")  # morador | gestor | visitante

    unidade = relationship("Unidade", back_populates="usuarios")
    cartoes = relationship("CartaoRFID", back_populates="usuario", cascade="all, delete-orphan")
    sessoes = relationship("SessaoRecarga", back_populates="usuario")


class CartaoRFID(Base):
    __tablename__ = "cartoes_rfid"

    id = Column(String(36), primary_key=True, default=_uuid)
    usuario_id = Column(String(36), ForeignKey("usuarios.id"), nullable=False)
    uid_fisico = Column(String(20), unique=True, nullable=False)
    status = Column(String(10), default="ativo")  # ativo | bloqueado | perdido

    usuario = relationship("Usuario", back_populates="cartoes")
    sessoes = relationship("SessaoRecarga", back_populates="cartao")


class PontoRecarga(Base):
    __tablename__ = "pontos_recarga"

    id = Column(String(36), primary_key=True, default=_uuid)
    modelo_equipamento = Column(String(50), default="GoodWe HCA G2")
    potencia_nominal_kw = Column(Integer, default=11)
    protocolo_conectividade = Column(String(20), default="OCPP 1.6")
    localizacao = Column(String(100), default="Estacionamento L1")

    sessoes = relationship("SessaoRecarga", back_populates="ponto")


class SessaoRecarga(Base):
    __tablename__ = "sessoes_recarga"

    id = Column(String(36), primary_key=True, default=_uuid)
    usuario_id = Column(String(36), ForeignKey("usuarios.id"), nullable=False)
    cartao_id = Column(String(36), ForeignKey("cartoes_rfid.id"), nullable=True)
    ponto_id = Column(String(36), ForeignKey("pontos_recarga.id"), nullable=False)
    inicio = Column(DateTime, nullable=False)
    fim = Column(DateTime, nullable=True)
    kwh_inicial = Column(Float, nullable=False)
    kwh_final = Column(Float, nullable=True)
    # concluida_normal | interrompida | em_andamento
    status_encerramento = Column(String(20), default="em_andamento")

    usuario = relationship("Usuario", back_populates="sessoes")
    cartao = relationship("CartaoRFID", back_populates="sessoes")
    ponto = relationship("PontoRecarga", back_populates="sessoes")
    eventos = relationship(
        "EventoTelemetria", back_populates="sessao", cascade="all, delete-orphan"
    )
    anomalia = relationship(
        "Anomalia", back_populates="sessao", uselist=False, cascade="all, delete-orphan"
    )
    itens_fatura = relationship("ItemFatura", back_populates="sessao")

    @property
    def kwh_consumido(self) -> float:
        """kWh líquido entregue na sessão."""
        if self.kwh_final is None or self.kwh_inicial is None:
            return 0.0
        return max(0.0, self.kwh_final - self.kwh_inicial)

    @property
    def duracao_horas(self) -> float:
        """Duração da sessão em horas."""
        if self.fim is None:
            return 0.0
        return (self.fim - self.inicio).total_seconds() / 3600

    @property
    def potencia_media_kw(self) -> float:
        """Potência média durante a sessão (kW)."""
        h = self.duracao_horas
        if h == 0:
            return 0.0
        return self.kwh_consumido / h


class EventoTelemetria(Base):
    """Leituras periódicas durante uma sessão (equivalente a MeterValues no OCPP)."""
    __tablename__ = "eventos_telemetria"

    id = Column(String(36), primary_key=True, default=_uuid)
    sessao_id = Column(String(36), ForeignKey("sessoes_recarga.id"), nullable=False)
    timestamp = Column(DateTime, nullable=False)
    potencia_kw = Column(Float, nullable=False)
    energia_acumulada_kwh = Column(Float, nullable=False)

    sessao = relationship("SessaoRecarga", back_populates="eventos")


class Anomalia(Base):
    __tablename__ = "anomalias"

    id = Column(String(36), primary_key=True, default=_uuid)
    sessao_id = Column(String(36), ForeignKey("sessoes_recarga.id"), nullable=False, unique=True)
    tipo = Column(String(60), nullable=False)
    score = Column(Float, nullable=False)           # 0.0 – 1.0, quanto maior mais anômalo
    status_revisao = Column(String(15), default="pendente")  # pendente | revisado | descartado
    observacao = Column(Text, nullable=True)

    sessao = relationship("SessaoRecarga", back_populates="anomalia")


class Fatura(Base):
    __tablename__ = "faturas"

    id = Column(String(36), primary_key=True, default=_uuid)
    unidade_id = Column(String(36), ForeignKey("unidades.id"), nullable=False)
    mes_referencia = Column(String(7), nullable=False)   # formato "YYYY-MM"
    componente_variavel = Column(Float, nullable=False)  # custo de energia consumida
    componente_fixo = Column(Float, nullable=False)      # rateio de manutenção
    total = Column(Float, nullable=False)
    gerada_em = Column(DateTime, default=datetime.utcnow)

    unidade = relationship("Unidade", back_populates="faturas")
    itens = relationship("ItemFatura", back_populates="fatura", cascade="all, delete-orphan")


class ItemFatura(Base):
    """Detalhe por sessão dentro de uma fatura — permite auditoria completa."""
    __tablename__ = "itens_fatura"

    id = Column(String(36), primary_key=True, default=_uuid)
    fatura_id = Column(String(36), ForeignKey("faturas.id"), nullable=False)
    sessao_id = Column(String(36), ForeignKey("sessoes_recarga.id"), nullable=False)
    kwh_atribuido = Column(Float, nullable=False)
    valor = Column(Float, nullable=False)

    fatura = relationship("Fatura", back_populates="itens")
    sessao = relationship("SessaoRecarga", back_populates="itens_fatura")
